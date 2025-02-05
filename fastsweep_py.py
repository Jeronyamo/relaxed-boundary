# From fastsweep repository

"""
This file is *not* installed as part of the module. It contains a pure python implementation
of the fast sweeping method. It (currently) is much slower than using the C++/CUDA version.

This file could be useful for debugging or development of further features.

"""

import time
import skfmm
import fastsweep

import drjit as dr
import numpy as np

# Use CUDA if possible, otherwise use LLVM
if dr.has_backend(dr.JitBackend.CUDA):
    from drjit.cuda import Array3f64 as Vector3f
    from drjit.cuda import Array3i as Vector3i
    from drjit.cuda import Bool, Float, Float64, Int32, TensorXf, TensorXf64
else:
    from drjit.llvm import Array3f64 as Vector3f
    from drjit.llvm import Array3i as Vector3i
    from drjit.llvm import Bool, Float, Float64, Int32, TensorXf, TensorXf64

from drjit.scalar import Array3f64 as ScalarVector3f
from drjit.scalar import Array3i as ScalarVector3i


BORDER_SIZE = 1


def idx(x, y, z, shape):
    return z * shape[1] * shape[0] + y * shape[0] + x


def initialize_distance(init_phi):
    # The implementation follows the code in https://github.com/scikit-fmm/scikit-fmm
    dims = 3
    shape_in = ScalarVector3i(init_phi.shape[0], init_phi.shape[1], init_phi.shape[2])
    shape = shape_in + 2 * BORDER_SIZE
    z, y, x = dr.meshgrid(*[dr.arange(Int32, shape[i]) for i in range(3)], indexing='ij')
    coord = Vector3i(x, y, z)
    ldistance = Vector3f(dr.inf)

    # Assume SDF is in [0,1]
    active = dr.all((coord >= BORDER_SIZE) & (coord < shape - BORDER_SIZE))
    linear_idx = idx(coord[0] - BORDER_SIZE, coord[1] - BORDER_SIZE, coord[2] - BORDER_SIZE, shape_in)
    init_phi_v = Float64(dr.gather(Float, init_phi.array, linear_idx, active))
    deltas = 1 / Vector3f(shape_in)
    borders = dr.zeros(Bool, dr.prod(shape))
    for dim in range(dims):
        for j in range(-1, 2, 2):
            offset_coord = Vector3i(coord)
            offset_coord[dim] += j
            valid = active & (offset_coord[dim] >= BORDER_SIZE) & (offset_coord[dim] < shape[dim] - BORDER_SIZE)
            init_phi_n = dr.gather(Float, init_phi.array, idx(offset_coord[0] - BORDER_SIZE,
                                                              offset_coord[1] - BORDER_SIZE,
                                                              offset_coord[2] - BORDER_SIZE,
                                                              shape_in), valid)
            valid &= init_phi_v * init_phi_n < 0
            borders |= valid
            d = deltas[dim] * init_phi_v / (init_phi_v - Float64(init_phi_n))
            ldistance[dim][valid & (ldistance[dim] > d)] = d
    print(ldistance[1, 100:120])

    # If we actually are on  the transition zone
    dsum = dr.sum(dr.select(ldistance > 0, 1 / dr.sqr(ldistance), 0))
    zero_init = active & dr.eq(init_phi_v, 0.0)
    distance = dr.select(zero_init | ~borders, 0.0, dr.sqrt(1 / dsum))
    frozen = Int32(dr.select(borders | zero_init, 1, 0))

    # High default value for non-frozen nodes
    distance[~(borders | zero_init)] = 90000
    return TensorXf64(distance, shape), frozen


def solve_eikonal(cur_dist, m, d):
    d = Vector3f(d)

    # Sort m and d according to m
    for i in range(1, 3):
        for j in range(3 - i):
            tmp_m = Float64(m[j])
            tmp_d = Float64(d[j])
            swap = m[j] > m[j + 1]
            m[j][swap] = m[j + 1]
            d[j][swap] = d[j + 1]
            m[j + 1][swap] = tmp_m
            d[j + 1][swap] = tmp_d

    # Solve the eikonal equation locally to update distance
    m2_0 = m[0] * m[0]
    m2_1 = m[1] * m[1]
    m2_2 = m[2] * m[2]
    d2_0 = d[0] * d[0]
    d2_1 = d[1] * d[1]
    d2_2 = d[2] * d[2]
    dist_new = m[0] + d[0]
    cond1 = dist_new > m[1]
    s = dr.sqrt(-m2_0 + 2 * m[0] * m[1] - m2_1 + d2_0 + d2_1)
    dist_new[cond1] = (m[1] * d2_0 + m[0] * d2_1 + d[0] * d[1] * s) / (d2_0 + d2_1)
    a = dr.sqrt(-m2_0 * d2_1 - m2_0 * d2_2 + 2 * m[0] * m[1] * d2_2 - m2_1 * d2_0 - m2_1 * d2_2 + 2 * m[0] * m[2] *
                d2_1 - m2_2 * d2_0 - m2_2 * d2_1 + 2 * m[1] * m[2] * d2_0 + d2_0 * d2_1 + d2_0 * d2_2 + d2_1 * d2_2)
    dist_new[cond1 & (dist_new > m[2])] = (m[2] * d2_0 * d2_1 + m[1] * d2_0 * d2_2 + m[0] * d2_1 *
                                           d2_2 + d[0] * d[1] * d[2] * a) / (d2_0 * d2_1 + d2_0 * d2_2 + d2_1 * d2_2)
    return dr.minimum(cur_dist, dist_new)


def sweep_step(x, y, distance, frozen, shape_in, level, sweep_offset, dx):
    shape = shape_in + 2 * BORDER_SIZE

    z = level - x - y
    active = (x <= shape_in.x) & (y <= shape_in.y) & (z > 0) & (z <= shape_in.z)

    i = dr.abs(z - sweep_offset.z)
    j = dr.abs(y - sweep_offset.y)
    k = dr.abs(x - sweep_offset.x)
    linear_idx = i * shape.y * shape.x + j * shape.x + k
    active &= dr.neq(dr.gather(Int32, frozen, linear_idx, active), 1)

    dist_data = distance.array
    center = dr.gather(Float64, dist_data, linear_idx, active)
    left = dr.gather(Float64, dist_data, linear_idx - 1, active)
    right = dr.gather(Float64, dist_data, linear_idx + 1, active)
    up = dr.gather(Float64, dist_data, linear_idx - shape.x, active)
    down = dr.gather(Float64, dist_data, linear_idx + shape.x, active)
    front = dr.gather(Float64, dist_data, linear_idx - shape.y * shape.x, active)
    back = dr.gather(Float64, dist_data, linear_idx + shape.y * shape.x, active)

    min_values = [dr.minimum(left, right), dr.minimum(up, down), dr.minimum(front, back)]
    eik = solve_eikonal(center, min_values, dx)

    # Update the current distance information
    dr.scatter(distance.array, eik, linear_idx, active=active)


def fast_sweep(distance, frozen, n_iter=1):
    shape_in = ScalarVector3i(*distance.shape) - 2 * BORDER_SIZE
    total_levels = sum(shape_in)
    dx = 1 / ScalarVector3f(shape_in)
    sweep_offsets = 2 * [ScalarVector3i(0, 0, 0),
                         ScalarVector3i(0, shape_in[1] + 1, 0),
                         ScalarVector3i(0, 0, shape_in[2] + 1),
                         ScalarVector3i(shape_in[0] + 1, 0, 0)]

    shape_in_opaque = Vector3i(shape_in)
    dr.make_opaque(shape_in_opaque)
    for i in range(n_iter):
        for sw_count in range(1, 9):
            if sw_count in [2, 5, 7, 8]:
                start = total_levels
                end = 2
                delta = -1
            else:
                start = 3
                end = total_levels + 1
                delta = 1

            sweep_offset = Vector3i(sweep_offsets[sw_count - 1])
            dr.make_opaque(sweep_offset)
            for level in range(start, end, delta):
                xs = max(1, level - (shape_in.y + shape_in.z))
                ys = max(1, level - (shape_in.x + shape_in.z))
                xe = min(shape_in.x, level - 2)
                ye = min(shape_in.y, level - 2)

                # Compute x and y sizes of the current sweep plane
                xr = xe - xs + 1
                yr = ye - ys + 1
                # x, y = dr.meshgrid(dr.arange(Int32, xr), dr.arange(Int32, yr))
                indices = dr.arange(Int32, xr * yr)
                xr_opaque = dr.opaque(Int32, xr)
                x = indices // xr_opaque
                y = indices % xr_opaque
                x = x + dr.opaque(Int32, xs)
                y = y + dr.opaque(Int32, ys)
                sweep_step(x, y, distance, frozen, shape_in_opaque,
                           dr.opaque(Int32, level), sweep_offset, dx)

                dr.sync_thread()


def redistance(phi):
    distance, frozen = initialize_distance(phi)
    for a in distance.numpy():
        for b in a:
            for c in b:
                print(f"{c}, ", end='')
            print()
        print()
    exit(0)
    dr.eval(distance, frozen)
    dr.sync_thread()
    fast_sweep(distance, frozen)

    # Remove border passing and multiply by sign of the input
    z, y, x = dr.meshgrid(dr.arange(Int32, phi.shape[0]) + BORDER_SIZE,
                          dr.arange(Int32, phi.shape[1]) + BORDER_SIZE,
                          dr.arange(Int32, phi.shape[2]) + BORDER_SIZE, indexing='ij')
    result = dr.gather(Float64, distance.array, z * distance.shape[0] * distance.shape[1] + y * distance.shape[0] + x)
    return TensorXf(result * dr.sign(phi.array), phi.shape)



def check_eikonal(arr: np.ndarray, dx: list[float]):
    # grad_x, grad_y, grad_z = np.gradient(arr)
    # grad_norm: np.ndarray = (grad_x ** 2 + grad_y ** 2 + grad_z ** 2) * 17 * 17
    # print(grad_norm.min(), grad_norm.max())
    # return
    g_mean, g_min, g_max = 0., 99999., -1.
    for i in range(arr.shape[0]):
        for j in range(arr.shape[1]):
            for k in range(arr.shape[2]):
                curr = arr[i, j, k]
                # i1, j1, k1 = i,j,k

                if i == 0: i_min = 1
                elif i == arr.shape[0] - 1: i_min = arr.shape[0] - 2
                else:
                    i_min = (i-1) if arr[i-1,j,k] <= arr[i+1,j,k] else (i+1)
                    # i1 = i+1 if i_min < i else i-1

                if j == 0: j_min = 1
                elif j == arr.shape[1] - 1: j_min = arr.shape[1] - 2
                else:
                    j_min = (j-1) if arr[i,j-1,k] <= arr[i,j+1,k] else (j+1)
                    # j1 = j+1 if j_min < j else j-1

                if k == 0: k_min = 1
                elif k == arr.shape[2] - 1: k_min = arr.shape[2] - 2
                else:
                    k_min = (k-1) if arr[i,j,k-1] <= arr[i,j,k+1] else (k+1)
                    # k1 = k+1 if k_min < k else k-1

                x_min, y_min, z_min = arr[i_min, j, k], arr[i, j_min, k], arr[i, j, k_min]
                # x_max, y_max, z_max = arr[i1, j, k], arr[i, j1, k], arr[i, j, k1]
                grad_eik = ((curr - x_min) / dx[0]) ** 2 + ((curr - y_min) / dx[1]) ** 2 + ((curr - z_min) / dx[2]) ** 2
                g_mean += grad_eik
                g_min = min(g_min, grad_eik)
                g_max = max(g_max, grad_eik)

                print(f"{grad_eik:.3f}, ", end='')
            print()
        print()
    g_mean /= arr.shape[0] * arr.shape[1] * arr.shape[2]
    print(g_min, g_max, g_mean)



if __name__ == "__main__":
    test_sdf = np.array([[[10., 10., 10., 10., 10.],
                          [10., 10.,-20., 10., 10.],
                          [10.,-20.,-30.,-20., 10.],
                          [10., 10.,-20., 10., 10.],
                          [10., 10., 10., 10., 10.]],
                         [[10., 10., 10., 10., 10.],
                          [10., 10.,-20., 10., 10.],
                          [10.,-20.,-30.,-20., 10.],
                          [10., 10.,-20., 10., 10.],
                          [10., 10., 10., 10., 10.]],
                         [[10., 10., 10., 10., 10.],
                          [10., 10.,-20., 10., 10.],
                          [10.,-20.,-30.,-20., 10.],
                          [10., 10.,-20., 10., 10.],
                          [10., 10., 10., 10., 10.]]])

    test_sdf = np.array([[[1., 1., .5, 1., 1.],
                          [1., .5,-.5, .5, 1.],
                          [.5,-.5,-.8,-.5, .5],
                          [1., .5,-.5, .5, 1.],
                          [1., 1., .5, 1., 1.]],
                         [[1., 1., .5, 1., 1.],
                          [1., .5,-.5, .5, 1.],
                          [.5,-.5,-.8,-.5, .5],
                          [1., .5,-.5, .5, 1.],
                          [1., 1., .5, 1., 1.]],
                         [[1., 1., .5, 1., 1.],
                          [1., .5,-.5, .5, 1.],
                          [.5,-.5,-.8,-.5, .5],
                          [1., .5,-.5, .5, 1.],
                          [1., 1., .5, 1., 1.]]])
    # test_sdf = np.zeros((17,17,17))
    # with open("./broken_array.txt") as broken_array_f:
    #     data = broken_array_f.read().split(' ')
    #     for i in range(17*17*17):
    #         test_sdf.flat[i] = float(data[i].strip())
    test_sdf = dr.cuda.TensorXf(test_sdf)
    test_sdf = fastsweep.redistance(test_sdf)
    # test_sdf = skfmm.distance(test_sdf, dx=[2./17,2./17,2./17])
    check_eikonal(test_sdf.numpy(), dx=[1./17,1./17,1./17])
    # print(test_sdf.shape)

    # for a in test_sdf:
    # # for a in test_sdf.numpy():
    #     for b in a:
    #         for c in b:
    #             print(f"{c}, ", end='')
    #         print()
    #     print()