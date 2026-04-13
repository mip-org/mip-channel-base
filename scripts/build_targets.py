#!/usr/bin/env python3
"""
CPU-level build target definitions and compiler flag mappings.

Packages with `simd: true` in their recipe.yaml are built once per CPU level
(x86_64_v1 through v4) on linux_x86_64 and windows_x86_64.  This module
centralises the level definitions, compiler-flag profiles, and helper
functions consumed by prepare_packages.py.

CI-only — never runs on end-user machines.
"""

from __future__ import annotations

from typing import Dict


CPU_LEVELS = (
    "x86_64_v1",
    "x86_64_v2",
    "x86_64_v3",
    "x86_64_v4",
)

CPU_LEVEL_ORDER = {level: idx for idx, level in enumerate(CPU_LEVELS, start=1)}

SIMD_ARCHITECTURES = {"linux_x86_64", "windows_x86_64"}

LTO_FLAG_GCC = "-flto=auto"
LTO_FLAG_MSVC = "/GL"
LTO_LINK_FLAG_MSVC = "/LTCG"

# GCC -march / -mtune per psABI level
LINUX_X86_64_CPU_PROFILES = {
    "x86_64_v1": {"march": "x86-64",    "mtune": "generic"},
    "x86_64_v2": {"march": "x86-64-v2", "mtune": "generic"},
    "x86_64_v3": {"march": "x86-64-v3", "mtune": "generic"},
    "x86_64_v4": {"march": "x86-64-v4", "mtune": "generic"},
}

# MSVC /arch / /favor per psABI level
WINDOWS_X86_64_CPU_PROFILES = {
    "x86_64_v1": {"arch_flag": "/arch:SSE2",   "favor_flag": "/favor:blend"},
    "x86_64_v2": {"arch_flag": "/arch:SSE4.2",  "favor_flag": "/favor:blend"},
    "x86_64_v3": {"arch_flag": "/arch:AVX2",    "favor_flag": "/favor:blend"},
    "x86_64_v4": {"arch_flag": "/arch:AVX512",  "favor_flag": "/favor:blend"},
}


def is_simd_architecture(architecture: str) -> bool:
    """Return True if *architecture* supports per-level SIMD builds."""
    return architecture in SIMD_ARCHITECTURES


def get_compiler_env(architecture: str, cpu_level: str) -> Dict[str, str]:
    """Return environment variables that set compiler flags for *cpu_level*.

    These are exported before compile scripts run so that cmake / mex / gcc
    pick up the correct -march or /arch flags automatically.
    """
    if cpu_level not in CPU_LEVELS:
        raise ValueError(f"Unknown cpu_level: {cpu_level}")

    env: Dict[str, str] = {"MIP_CPU_LEVEL": cpu_level}

    if architecture == "linux_x86_64":
        p = LINUX_X86_64_CPU_PROFILES[cpu_level]
        base = (
            f"-O3 -march={p['march']} -mtune={p['mtune']} {LTO_FLAG_GCC}"
        )
        env.update({
            "MIP_MARCH": p["march"],
            "MIP_MTUNE": p["mtune"],
            "CFLAGS": base,
            "CXXFLAGS": base,
            "FFLAGS": f"-O3 -march={p['march']} -mtune={p['mtune']} {LTO_FLAG_GCC}",
            "LDFLAGS": LTO_FLAG_GCC,
        })

    elif architecture == "windows_x86_64":
        p = WINDOWS_X86_64_CPU_PROFILES[cpu_level]
        cl_flags = f"/O2 /Oi {p['arch_flag']} {p['favor_flag']} {LTO_FLAG_MSVC}".strip()
        env.update({
            "CL": cl_flags,
            "_LINK_": LTO_LINK_FLAG_MSVC,
        })

    return env
