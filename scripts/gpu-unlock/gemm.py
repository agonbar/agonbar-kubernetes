"""Tensor-core throughput of one GPU, to see what gpu-unlock changes.

Runs inside any CUDA PyTorch image (bench.sh uses docling-serve's) with the
card under test as its only device. Prints one JSON object.

FP16/BF16/TF32 with FP32 accumulation are what GeForce Ampere runs at half
rate and gpu-unlock lifts. FP32 (no tensor cores) and INT8 are the controls:
they should not move.
"""
import json
import time

import torch

N = 8192
ITERS = 30


def timed(fn, iters=ITERS):
    for _ in range(5):
        fn()
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    torch.cuda.synchronize()
    return (time.perf_counter() - t0) / iters


def gemm(dtype, tf32=False):
    torch.backends.cuda.matmul.allow_tf32 = tf32
    a = torch.randn(N, N, device="cuda").to(dtype)
    b = torch.randn(N, N, device="cuda").to(dtype)
    return 2 * N**3 / timed(lambda: a @ b) / 1e12


def int8():
    a = torch.randint(-128, 127, (N, N), device="cuda", dtype=torch.int8)
    b = torch.randint(-128, 127, (N, N), device="cuda", dtype=torch.int8)
    return 2 * N**3 / timed(lambda: torch._int_mm(a, b)) / 1e12


def attention(dtype):
    # Prefill-shaped: 8 sequences x 32 heads x 4096 tokens x head_dim 128, causal.
    q, k, v = (torch.randn(8, 32, 4096, 128, device="cuda", dtype=dtype) for _ in range(3))
    f = lambda: torch.nn.functional.scaled_dot_product_attention(q, k, v, is_causal=True)
    return 4 * 8 * 32 * 4096**2 * 128 / 2 / timed(f, 20) / 1e12


def power():
    # NVML through ctypes: the CDI spec mounts libnvidia-ml, pynvml is not in the image.
    import ctypes

    try:
        nvml = ctypes.CDLL("libnvidia-ml.so.1")
        nvml.nvmlInit_v2()
        h, w = ctypes.c_void_p(), ctypes.c_uint()
        nvml.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h))
        nvml.nvmlDeviceGetPowerManagementLimit(h, ctypes.byref(w))
        return {"limit_w": w.value / 1000}
    except OSError as e:
        return {"error": str(e)}


out = {"device": torch.cuda.get_device_name(0), "torch": torch.__version__, "power": power(), "tflops": {}}
for name, fn in [
    ("fp16", lambda: gemm(torch.float16)),
    ("bf16", lambda: gemm(torch.bfloat16)),
    ("tf32", lambda: gemm(torch.float32, tf32=True)),
    ("fp32", lambda: gemm(torch.float32)),
    ("int8", int8),
    ("attn_fp16", lambda: attention(torch.float16)),
    ("attn_bf16", lambda: attention(torch.bfloat16)),
]:
    out["tflops"][name] = round(fn(), 2)
    torch.cuda.empty_cache()
print(json.dumps(out))
