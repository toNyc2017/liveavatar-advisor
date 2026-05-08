"""
Simplified Single Premium Immediate Annuity (SPIA) calculator.

Purpose: produce illustrative income estimates for a fixed-income annuity
based on premium, age, gender, and payout structure. Uses industry-average
payout rates indexed to a typical mid-cycle interest rate environment
(roughly 2025–2026: 10-year Treasury ~4–4.5%).

This is NOT a binding quote. Real carrier rates vary by:
  - The specific insurance company's mortality assumptions
  - Their cost of capital and profit margins
  - The state where the policy is issued
  - Current credit quality of the carrier
  - Add-on features (cash refund, inflation rider, etc.)

Output should always be labeled illustrative / educational.

Usage:
    from spia_calculator import calculate_spia
    result = calculate_spia(
        premium=500_000,
        age=65,
        gender="male",
        payout_type="life_only",
    )
    print(result["monthly_income"])  # → e.g. 2667.0

CLI:
    python spia_calculator.py --premium 500000 --age 65 --gender male
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal


# ---- payout-rate tables ----------------------------------------------------
#
# Annual payout as % of premium for a single-life immediate annuity, no period
# certain, lump-sum funding, at typical mid-cycle interest rates. Based on
# composite of public quotes from major US carriers (Pacific Life, Nationwide,
# MassMutual, NY Life, Mutual of Omaha) circa 2025–2026.
#
# Values are interpolated linearly between listed ages.

_LIFE_ONLY_MALE = {
    55: 5.20, 60: 5.80, 65: 6.40, 70: 7.20, 75: 8.30, 80: 9.70, 85: 11.50, 90: 13.80,
}
_LIFE_ONLY_FEMALE = {
    55: 4.95, 60: 5.50, 65: 6.00, 70: 6.70, 75: 7.70, 80: 8.90, 85: 10.40, 90: 12.30,
}

# Multipliers applied to the life-only base rate for other payout structures.
# Values represent the payout as a fraction of life-only at the same age/gender.
_PAYOUT_TYPE_FACTOR = {
    "life_only": 1.00,
    "life_10yr_certain": 0.94,    # 10-year period certain
    "life_20yr_certain": 0.85,    # 20-year period certain
    "life_with_cash_refund": 0.92,  # death benefit returns unused premium
    "joint_life_100": 0.82,        # spouse continues at 100% on death
    "joint_life_50": 0.90,         # spouse continues at 50% on death
    "period_certain_10yr": 0.62,   # pure 10-year, no life component
    "period_certain_20yr": 0.42,   # pure 20-year, no life component
}

# Simplified life expectancy in years from current age (Social Security 2021
# period life table, rounded). Used to estimate total lifetime income.
_LIFE_EXPECTANCY_MALE = {
    55: 24.5, 60: 20.5, 65: 17.0, 70: 13.5, 75: 10.5, 80: 8.0, 85: 6.0, 90: 4.5,
}
_LIFE_EXPECTANCY_FEMALE = {
    55: 28.0, 60: 24.0, 65: 20.0, 70: 16.0, 75: 12.5, 80: 9.5, 85: 7.0, 90: 5.5,
}


PayoutType = Literal[
    "life_only",
    "life_10yr_certain",
    "life_20yr_certain",
    "life_with_cash_refund",
    "joint_life_100",
    "joint_life_50",
    "period_certain_10yr",
    "period_certain_20yr",
]
Gender = Literal["male", "female"]


@dataclass
class SpiaResult:
    """Structured output from a SPIA calculation."""
    # Inputs (echoed back for reference)
    premium: float
    age: int
    gender: str
    payout_type: str
    payout_type_label: str

    # Payout rates and amounts
    payout_rate_pct: float          # e.g. 6.4 means 6.4% annual payout
    monthly_income: float           # dollars per month
    annual_income: float            # dollars per year

    # Lifetime estimates (illustrative)
    life_expectancy_years: float
    estimated_lifetime_income: float

    # Period-certain guarantee (if applicable)
    guaranteed_years: int | None
    guaranteed_total: float | None

    # Metadata
    calculation_date: str
    disclaimer: str


# Human-readable labels for payout types (for display).
_PAYOUT_TYPE_LABEL = {
    "life_only": "Life Only (single life, no death benefit)",
    "life_10yr_certain": "Life with 10-Year Period Certain",
    "life_20yr_certain": "Life with 20-Year Period Certain",
    "life_with_cash_refund": "Life with Cash Refund (unused premium returned at death)",
    "joint_life_100": "Joint Life — 100% to Survivor",
    "joint_life_50": "Joint Life — 50% to Survivor",
    "period_certain_10yr": "10-Year Period Certain Only (no life component)",
    "period_certain_20yr": "20-Year Period Certain Only (no life component)",
}


def _interpolate(table: dict[int, float], age: int) -> float:
    """Linear interpolation across a sparse age table."""
    ages = sorted(table.keys())
    if age <= ages[0]:
        return table[ages[0]]
    if age >= ages[-1]:
        return table[ages[-1]]
    # Find the bracketing pair
    for i in range(len(ages) - 1):
        a0, a1 = ages[i], ages[i + 1]
        if a0 <= age <= a1:
            v0, v1 = table[a0], table[a1]
            t = (age - a0) / (a1 - a0)
            return v0 + t * (v1 - v0)
    return table[ages[-1]]


def calculate_spia(
    premium: float,
    age: int,
    gender: Gender,
    payout_type: PayoutType = "life_only",
) -> dict:
    """
    Estimate fixed-income annuity payouts.

    Parameters
    ----------
    premium : float
        Lump sum invested in the annuity (USD).
    age : int
        Age of the annuitant at start of payments.
    gender : "male" | "female"
        Used to adjust mortality assumption (males ~3 yrs shorter avg lifespan
        means slightly higher payout per dollar premium).
    payout_type : str
        Structure of the income stream. Defaults to "life_only".

    Returns
    -------
    dict
        Structured result; see SpiaResult dataclass.
    """
    # --- Input validation ---
    if premium <= 0:
        raise ValueError("Premium must be positive.")
    if not (40 <= age <= 95):
        raise ValueError(f"Age {age} outside supported range 40–95.")
    if gender not in ("male", "female"):
        raise ValueError(f"Gender must be 'male' or 'female', got '{gender}'.")
    if payout_type not in _PAYOUT_TYPE_FACTOR:
        raise ValueError(
            f"Unknown payout_type '{payout_type}'. Valid: {list(_PAYOUT_TYPE_FACTOR)}"
        )

    # --- Look up base rate ---
    base_table = _LIFE_ONLY_MALE if gender == "male" else _LIFE_ONLY_FEMALE
    base_rate = _interpolate(base_table, age)

    # --- Adjust for payout type ---
    rate_pct = base_rate * _PAYOUT_TYPE_FACTOR[payout_type]
    annual_income = premium * (rate_pct / 100.0)
    monthly_income = annual_income / 12.0

    # --- Life expectancy estimate (illustrative lifetime income) ---
    life_table = _LIFE_EXPECTANCY_MALE if gender == "male" else _LIFE_EXPECTANCY_FEMALE
    life_expectancy = _interpolate(life_table, age)
    estimated_lifetime_income = annual_income * life_expectancy

    # --- Period-certain guarantee (if applicable) ---
    guaranteed_years: int | None = None
    guaranteed_total: float | None = None
    if "10yr" in payout_type:
        guaranteed_years = 10
        guaranteed_total = annual_income * 10
    elif "20yr" in payout_type:
        guaranteed_years = 20
        guaranteed_total = annual_income * 20
    elif payout_type == "life_with_cash_refund":
        # Cash refund: the death benefit returns any unused premium. Total
        # guaranteed minimum is the original premium.
        guaranteed_total = premium

    result = SpiaResult(
        premium=round(premium, 2),
        age=age,
        gender=gender,
        payout_type=payout_type,
        payout_type_label=_PAYOUT_TYPE_LABEL[payout_type],
        payout_rate_pct=round(rate_pct, 3),
        monthly_income=round(monthly_income, 2),
        annual_income=round(annual_income, 2),
        life_expectancy_years=round(life_expectancy, 1),
        estimated_lifetime_income=round(estimated_lifetime_income, 2),
        guaranteed_years=guaranteed_years,
        guaranteed_total=round(guaranteed_total, 2) if guaranteed_total else None,
        calculation_date=datetime.utcnow().strftime("%Y-%m-%d"),
        disclaimer=(
            "Illustrative only. Based on industry-average payout rates as of 2025–2026. "
            "Actual quotes vary by insurance carrier, state of issue, current interest "
            "rate environment, carrier credit quality, and policy features. This is not "
            "a binding offer or recommendation. Consult a licensed annuity professional "
            "before making any purchase decision."
        ),
    )
    return asdict(result)


def format_for_display(result: dict) -> str:
    """Pretty-print a SPIA result as plain text suitable for display or download."""
    lines = [
        "═══ Annuity Income Estimate ═══",
        "",
        f"  Premium invested      : ${result['premium']:,.2f}",
        f"  Annuitant age         : {result['age']} ({result['gender']})",
        f"  Payout structure      : {result['payout_type_label']}",
        "",
        "  Estimated income:",
        f"    Annual              : ${result['annual_income']:,.2f}",
        f"    Monthly             : ${result['monthly_income']:,.2f}",
        f"    Payout rate         : {result['payout_rate_pct']:.2f}% of premium per year",
        "",
        f"  Estimated life expectancy at this age: {result['life_expectancy_years']} years",
        f"  Total lifetime income (illustrative): ${result['estimated_lifetime_income']:,.2f}",
    ]
    if result.get("guaranteed_years"):
        lines.append("")
        lines.append(f"  Guaranteed for first {result['guaranteed_years']} years even on early death:")
        lines.append(f"    ${result['guaranteed_total']:,.2f} total")
    elif result.get("guaranteed_total"):
        lines.append("")
        lines.append(f"  Cash refund minimum (returns unused premium at death):")
        lines.append(f"    ${result['guaranteed_total']:,.2f}")
    lines += [
        "",
        f"  Calculation date: {result['calculation_date']}",
        "",
        "  ─── Important ───",
        f"  {result['disclaimer']}",
    ]
    return "\n".join(lines)


def _cli() -> None:
    p = argparse.ArgumentParser(description="Estimate SPIA income.")
    p.add_argument("--premium", type=float, required=True, help="Lump sum invested (USD)")
    p.add_argument("--age", type=int, required=True, help="Annuitant age (40–95)")
    p.add_argument("--gender", choices=("male", "female"), required=True)
    p.add_argument(
        "--payout-type",
        choices=list(_PAYOUT_TYPE_FACTOR),
        default="life_only",
        help="Income structure (default: life_only)",
    )
    p.add_argument("--json", action="store_true", help="Output JSON instead of text")
    args = p.parse_args()

    result = calculate_spia(
        premium=args.premium,
        age=args.age,
        gender=args.gender,
        payout_type=args.payout_type,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_for_display(result))


if __name__ == "__main__":
    _cli()
