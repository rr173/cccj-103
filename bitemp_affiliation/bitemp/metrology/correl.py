"""相关矩阵装配与校验。

约定：
  - correlations.csv 给出非对角相关系数 rho；未给出的非对角项按 0 处理
    （本程序约定，随输出说明），对角线恒为 1；
  - 同一无序变量对的重复项（含反向 (A,B)/(B,A)）：rho 完全一致时合并，
    不一致时拒收该 specimen；
  - rho 必须有限且在 [-1, 1]；
  - 矩阵整体必须半正定（PSD），不能只逐对检查：以最小特征值
    >= -EIG_TOL（1e-10）为准；落在 [-EIG_TOL, 0) 的负特征值按 0 修正
    （截断负特征值重构矩阵），并在结果中注明，不静默改出正定矩阵；
    小于 -EIG_TOL 则拒收该 specimen。

特征值用循环 Jacobi 法（对称矩阵）计算，纯 Python 实现，不依赖 numpy。
"""
from __future__ import annotations

import math

EIG_TOL = 1e-10  # 半正定判定的浮点容差（最小特征值 >= -EIG_TOL 视为合法）


class CorrError(ValueError):
    """相关矩阵非法（逐 specimen 拒收原因）。line 为可定位的输入行号（可空）。"""

    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.line = line


def assemble_rho(entries: list[tuple[str, str, float, int]]) -> dict[tuple[str, str], float]:
    """把 (left, right, rho, line) 列表合并成无序对 -> rho 的字典。

    反向/同向重复项一致（浮点完全相等）时合并，不一致抛 CorrError。
    """
    pair: dict[tuple[str, str], tuple[float, int]] = {}
    for left, right, rho, line in entries:
        if not math.isfinite(rho):
            raise CorrError(f"correlations.csv 第 {line} 行: rho 必须有限，实际为 {rho!r}",
                            line)
        if not -1.0 <= rho <= 1.0:
            raise CorrError(f"correlations.csv 第 {line} 行: rho 超出 [-1, 1]: {rho!r}",
                            line)
        if left == right:
            if rho != 1.0:
                raise CorrError(
                    f"correlations.csv 第 {line} 行: 对角线 ({left},{right}) 的 rho 必须为 1，"
                    f"实际为 {rho!r}", line)
            continue  # 对角线恒为 1，一致的重复说明直接忽略
        key = (left, right) if left <= right else (right, left)
        if key in pair:
            old, old_line = pair[key]
            if old != rho:
                raise CorrError(
                    f"变量对 ({key[0]}, {key[1]}) 的相关系数重复且不一致: "
                    f"第 {old_line} 行为 {old!r}，第 {line} 行为 {rho!r}", line)
            continue  # 一致的重复项（含反向）合并
        pair[key] = (rho, line)
    return {k: v for k, (v, _line) in pair.items()}


def correlation_matrix(var_names: list[str],
                       rho_pairs: dict[tuple[str, str], float]) -> list[list[float]]:
    """按 var_names 顺序装配相关矩阵 R：对角 1，缺失的非对角项按 0。"""
    n = len(var_names)
    idx = {name: i for i, name in enumerate(var_names)}
    r = [[0.0] * n for _ in range(n)]
    for i in range(n):
        r[i][i] = 1.0
    for (a, b), rho in rho_pairs.items():
        if a not in idx or b not in idx:
            continue  # 未测量的变量对不参与矩阵（装配前已校验名字合法）
        i, j = idx[a], idx[b]
        r[i][j] = rho
        r[j][i] = rho
    return r


def jacobi_eigh(a: list[list[float]], *, max_sweeps: int = 100,
                tol: float = 1e-15) -> tuple[list[float], list[list[float]]]:
    """对称矩阵特征分解：返回 (特征值列表, 特征向量列组成的矩阵 V)。

    V[i][j] 是第 j 个特征向量的第 i 个分量；a = V diag(evals) V^T。
    经典循环 Jacobi 法，适合本程序的小矩阵（每 specimen 的原始变量数）。
    """
    n = len(a)
    if n == 0:
        return [], []
    a = [row[:] for row in a]
    v = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    if n == 1:
        return [a[0][0]], v

    scale = max(1.0, max(abs(a[i][i]) for i in range(n)))
    for _sweep in range(max_sweeps):
        off = math.sqrt(sum(a[p][q] ** 2 for p in range(n) for q in range(p + 1, n)))
        if off <= tol * scale:
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                apq = a[p][q]
                if apq == 0.0:
                    continue
                app, aqq = a[p][p], a[q][q]
                theta = (aqq - app) / (2.0 * apq)
                t = (1.0 if theta >= 0.0 else -1.0) / (abs(theta) + math.sqrt(theta * theta + 1.0))
                c = 1.0 / math.sqrt(t * t + 1.0)
                s = t * c
                for k in range(n):
                    if k != p and k != q:
                        akp, akq = a[k][p], a[k][q]
                        a[k][p] = a[p][k] = c * akp - s * akq
                        a[k][q] = a[q][k] = s * akp + c * akq
                a[p][p] = app - t * apq
                a[q][q] = aqq + t * apq
                a[p][q] = a[q][p] = 0.0
                for k in range(n):
                    vkp, vkq = v[k][p], v[k][q]
                    v[k][p] = c * vkp - s * vkq
                    v[k][q] = s * vkp + c * vkq
    return [a[i][i] for i in range(n)], v


def check_and_fix_psd(r: list[list[float]]) -> tuple[list[list[float]], list[str]]:
    """校验相关矩阵半正定；容差内负特征值按 0 修正并注明。

    返回 (可用的矩阵, 注记列表)。最小特征值 < -EIG_TOL 时抛 CorrError。
    """
    n = len(r)
    if n <= 1:
        return r, []
    evals, vecs = jacobi_eigh(r)
    lam_min = min(evals)
    if lam_min < -EIG_TOL:
        raise CorrError(
            f"相关矩阵非半正定: 最小特征值 {lam_min:.6e} < -{EIG_TOL:.0e} "
            f"（各对 rho 单独合法不代表整体可成立）")
    if lam_min >= 0.0:
        return r, []
    # 容差内的负特征值按 0 修正：R' = V max(λ,0) V^T，并显式注明
    clipped = [max(lam, 0.0) for lam in evals]
    fixed = [[0.0] * n for _ in range(n)]
    for j in range(n):
        lam = clipped[j]
        if lam == 0.0:
            continue
        for p in range(n):
            vpj = vecs[p][j]
            if vpj == 0.0:
                continue
            for q in range(n):
                fixed[p][q] += lam * vpj * vecs[q][j]
    note = (f"相关矩阵最小特征值 {lam_min:.3e} 在容差 [-{EIG_TOL:.0e}, 0) 内，"
            f"已按 0 修正（截断负特征值后重构矩阵）")
    return fixed, [note]


def covariance_from(r: list[list[float]], u_si: list[float]) -> list[list[float]]:
    """由相关矩阵 R 与各原始量标准不确定度合成协方差矩阵 C = D R D。"""
    n = len(r)
    return [[r[i][j] * u_si[i] * u_si[j] for j in range(n)] for i in range(n)]
