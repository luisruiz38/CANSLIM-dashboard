import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
import anthropic
import os

# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CANSLIM Dashboard — Stock Fundamentals",
    layout="wide",
    page_icon="📊",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
    /* Remove Streamlit chrome so we get full viewport */
    header, footer, #MainMenu { visibility: hidden; height: 0; }
    .stDeployButton { display: none !important; }
    .block-container { padding-top: 0.6rem !important; padding-bottom: 0.2rem !important; }

    /* Compact metrics */
    [data-testid="stMetricValue"] { font-size: 0.95rem !important; font-weight: 700 !important; line-height: 1.2 !important; }
    [data-testid="stMetricLabel"] { font-size: 0.6rem !important; color: #a6adc8 !important; margin-bottom: 0 !important; }
    div[data-testid="stMetric"]   { background: #181825; border-radius: 5px; padding: 5px 8px !important; }

    /* Section labels */
    .sec { font-size: 0.7rem; font-weight: 700; color: #89b4fa; text-transform: uppercase;
           letter-spacing: 0.07em; border-bottom: 1px solid #313244;
           padding-bottom: 2px; margin: 4px 0 3px 0; }

    /* CANSLIM signal badges */
    .sig  { display:inline-block; border-radius:4px; padding:2px 9px;
            font-size:0.69rem; font-weight:700; margin:2px 3px; }
    .g    { background:#1a3328; color:#a6e3a1; border:1px solid #a6e3a1; }
    .r    { background:#381a1a; color:#f38ba8; border:1px solid #f38ba8; }
    .y    { background:#383018; color:#f9e2af; border:1px solid #f9e2af; }

    /* Tighter column gaps */
    [data-testid="column"] { padding-left: 3px !important; padding-right: 3px !important; }

    /* Dividers */
    hr { margin: 3px 0 !important; }

    /* Expander */
    details > summary { font-size: 0.78rem !important; padding: 4px 8px !important; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def safe_get(d, key, default=None):
    v = d.get(key, default)
    return v if v not in (None, "None", "") else default

def fv(v, prefix="", suffix="", dec=1):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "N/A"
    return f"{prefix}{v:,.{dec}f}{suffix}"

def fl(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "N/A"
    if abs(v) >= 1e12: return f"${v/1e12:.2f}T"
    if abs(v) >= 1e9:  return f"${v/1e9:.2f}B"
    if abs(v) >= 1e6:  return f"${v/1e6:.1f}M"
    return f"${v:,.0f}"

def qlabel(dt):
    dt = pd.Timestamp(dt)
    return f"Q{(dt.month-1)//3+1}'{str(dt.year)[2:]}"

def badge(label, val, css):
    return f'<span class="sig {css}">{label}: {val}</span>'

def yoy_pct(series: pd.Series) -> pd.Series:
    """
    YoY % change vs. same quarter one year prior (offset=4).
    Uses abs(prior) as the denominator so that transitions from negative
    to positive (or vice-versa) are signed correctly:
      prior=-10, current=+2  →  +120%  (turnaround, positive)
      prior=+10, current=+12 →  +20%   (normal growth)
      prior=+10, current=-2  →  -120%  (collapse, negative)
    Returns NaN wherever the prior value is zero or unavailable.
    """
    prior = series.shift(4)
    change = series - prior
    denom  = prior.abs().replace(0, float("nan"))
    return (change / denom) * 100


# ─────────────────────────────────────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=1800, show_spinner=False)
def load_data(sym):
    try:
        tk = yf.Ticker(sym)
        info = tk.info or {}
    except Exception as e:
        if "rate" in str(e).lower() or "429" in str(e):
            raise RuntimeError("RATE_LIMIT")
        raise
    qis  = tk.quarterly_income_stmt
    bs   = tk.balance_sheet
    cf   = tk.cashflow
    try:
        ed = tk.earnings_dates
    except Exception:
        ed = None
    try:
        ih = tk.institutional_holders   # pctChange column = 13F accumulation signal
    except Exception:
        ih = None
    return info, qis, ed, bs, cf, ih


@st.cache_data(ttl=3600, show_spinner=False)
def calc_rs_rating(sym):
    """52-week relative strength vs S&P 500. Returns (relative_pct, stock_ret, sp500_ret)."""
    import datetime
    end   = datetime.date.today()
    start = end - datetime.timedelta(days=370)
    try:
        raw = yf.download([sym, "^GSPC"], start=start, end=end,
                          progress=False, auto_adjust=True)
        closes = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
        if sym not in closes.columns or "^GSPC" not in closes.columns:
            return None
        s  = closes[sym].dropna()
        sp = closes["^GSPC"].dropna()
        if len(s) < 10 or len(sp) < 10:
            return None
        sr = (s.iloc[-1] / s.iloc[0] - 1) * 100
        mr = (sp.iloc[-1] / sp.iloc[0] - 1) * 100
        return round(float(sr - mr), 1), round(float(sr), 1), round(float(mr), 1)
    except Exception:
        return None


def calc_de_trend(bs):
    """Returns (current_de, change, n_years, series) or None. D/E = ratio × 100."""
    if bs is None or bs.empty:
        return None
    debt_s = eq_s = None
    for lbl in ["Total Debt", "Long Term Debt And Capital Lease Obligation", "Long Term Debt"]:
        if lbl in bs.index:
            debt_s = bs.loc[lbl].dropna()
            break
    for lbl in ["Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"]:
        if lbl in bs.index:
            eq_s = bs.loc[lbl].dropna()
            break
    if debt_s is None or eq_s is None:
        return None
    common = debt_s.index.intersection(eq_s.index)
    if len(common) < 2:
        return None
    de_series = (debt_s[common] / eq_s[common] * 100).sort_index()
    de_series = de_series[de_series.abs() < 50_000].tail(3)
    if len(de_series) < 2:
        return None
    current = de_series.iloc[-1]
    oldest  = de_series.iloc[0]
    return current, current - oldest, len(de_series) - 1, de_series


def calc_buyback_yield(cf, market_cap):
    """
    Returns (latest_yield_pct, trend_str, net_series) using up to 5 years of data.
    trend: 'growing' | 'flat' | 'declining' | 'stopped' | 'net_issuance'
    Net buybacks = repurchases − issuances (positive = net cash returned to shareholders).
    """
    if cf is None or cf.empty or not market_cap:
        return None
    rep_s = None
    for lbl in ["Repurchase Of Capital Stock", "Net Common Stock Issuance",
                "Common Stock Repurchased"]:
        if lbl in cf.index:
            rep_s = cf.loc[lbl].dropna().sort_index()
            break
    if rep_s is None:
        return None
    # Repurchases arrive as negative; flip to positive = cash spent buying back stock
    net = (-rep_s).copy()
    if "Issuance Of Capital Stock" in cf.index:
        iss = cf.loc["Issuance Of Capital Stock"].dropna()
        for idx in net.index.intersection(iss.index):
            net[idx] -= iss[idx]
    net = net.tail(5)
    if net.empty:
        return None
    latest     = net.iloc[-1]
    yield_pct  = (latest / market_cap) * 100
    if len(net) >= 3:
        recent = net.tail(2).mean()
        older  = net.iloc[:-2].mean()
        if recent <= 0:
            trend = "net_issuance"
        elif older <= 0:
            trend = "growing"
        elif recent >= older * 1.1:
            trend = "growing"
        elif recent >= older * 0.85:
            trend = "flat"
        else:
            trend = "declining"
    else:
        trend = "growing" if latest > 0 else "net_issuance"
    return yield_pct, trend, net


def calc_qoq_accel(eps_df):
    """
    Sequential QoQ EPS acceleration with smoothing via weighted linear regression.

    Weights are exponential (2^i) so the most recent period carries the most
    influence. The slope of the weighted trend line is the primary signal, which
    means a single interior dip in an otherwise upward move does NOT cancel
    acceleration. The 'latest is peak' check acts only as a tiebreaker when
    the slope is near zero.

    Returns 'accelerating' | 'decelerating' | 'mixed' | None.
    """
    if eps_df is None or len(eps_df) < 4:
        return None

    # Pull up to 6 EPS values → up to 5 QoQ rates; wider window = smoother signal
    use_n = min(6, len(eps_df))
    vals  = eps_df["EPS"].tail(use_n).tolist()

    qoq = []
    for i in range(1, len(vals)):
        p, c = vals[i - 1], vals[i]
        if pd.isna(p) or pd.isna(c) or p == 0:
            qoq.append(None)
        else:
            qoq.append((c - p) / abs(p) * 100)

    valid = [q for q in qoq if q is not None]
    if len(valid) < 3:
        return None

    window = valid[-4:] if len(valid) >= 4 else valid   # up to 4 QoQ periods
    n      = len(window)
    latest = window[-1]

    # Exponential weights: oldest=1, next=2, …, most recent=2^(n-1)
    weights = [2 ** i for i in range(n)]
    x       = list(range(n))

    w_sum   = sum(weights)
    x_wm    = sum(weights[i] * x[i]      for i in range(n)) / w_sum
    y_wm    = sum(weights[i] * window[i] for i in range(n)) / w_sum
    num     = sum(weights[i] * (x[i] - x_wm) * (window[i] - y_wm) for i in range(n))
    den     = sum(weights[i] * (x[i] - x_wm) ** 2                  for i in range(n))
    slope   = num / den if den != 0 else 0.0

    # Secondary checks — used as tiebreaker when slope ≈ 0
    latest_is_peak   = latest >= max(window) * 0.92   # within 8 % of window high
    latest_is_trough = latest <= min(window) * 1.08   # within 8 % of window low

    if slope > 0:
        return "accelerating"
    if slope < 0:
        return "decelerating"
    # Slope is flat — fall back to latest position
    if latest_is_peak:
        return "accelerating"
    if latest_is_trough:
        return "decelerating"
    return "mixed"


def calc_institutional_signal(ih, info):
    """
    Aggregates pctChange from institutional_holders (13F filings) to gauge
    whether institutions are net accumulating or distributing.
    Returns (trend_str, inst_pct, pos_holders, neg_holders) or None.
    """
    inst_raw = info.get("heldPercentInstitutions")
    inst_pct = round(inst_raw * 100, 1) if inst_raw else None

    if ih is None or ih.empty or "pctChange" not in ih.columns:
        return "unknown", inst_pct, None, None

    chg = ih["pctChange"].dropna()
    chg = chg[chg.abs() < 0.5]   # exclude new positions (pctChange ≈ 1.0)
    if len(chg) < 5:
        return "insufficient", inst_pct, None, None

    pos = int((chg >  0.005).sum())
    neg = int((chg < -0.005).sum())
    ratio = pos / len(chg)

    if ratio >= 0.55:
        trend = "accumulating"
    elif ratio <= 0.40:
        trend = "distributing"
    else:
        trend = "mixed"
    return trend, inst_pct, pos, neg


def calc_insider_pct(info):
    """Returns insider ownership as a percentage, or None."""
    v = info.get("heldPercentInsiders")
    return round(v * 100, 2) if v is not None else None


def quarterly_df(qis, labels, col):
    """Return df with [Quarter, col, YoY(%)], up to 12 quarters — for Revenue."""
    if qis is None or qis.empty:
        return None
    series = None
    for lbl in labels:
        if lbl in qis.index:
            series = qis.loc[lbl].dropna()
            break
    if series is None or len(series) < 2:
        return None
    df = pd.DataFrame({"Date": pd.to_datetime(series.index), col: series.values})
    df = df.sort_values("Date").reset_index(drop=True)
    df["YoY (%)"] = yoy_pct(df[col])
    df["Quarter"] = df["Date"].apply(qlabel)
    return df.tail(12).reset_index(drop=True)


def build_eps_df(qis, ed):
    """
    EPS quarterly data for up to 12 quarters.
    Uses quarterly_income_stmt for the most recent quarters (more accurate)
    and supplements with earnings_dates for older history when needed.
    earnings_dates announcement dates are shifted back ~45 days to approximate
    the fiscal quarter-end date used for labelling.
    """
    # ── Pull from income statement ──────────────────────────────────────────
    qis_df = None
    if qis is not None and not qis.empty:
        for lbl in ["Basic EPS", "Diluted EPS"]:
            if lbl in qis.index:
                s = qis.loc[lbl].dropna()
                if len(s) > 0:
                    qis_df = pd.DataFrame({
                        "Date": pd.to_datetime(s.index).tz_localize(None),
                        "EPS":  s.values,
                    }).sort_values("Date")
                break

    # ── Pull from earnings_dates ────────────────────────────────────────────
    ed_df = None
    if ed is not None and not ed.empty and "Reported EPS" in ed.columns:
        s = ed["Reported EPS"].dropna()
        # Strip timezone so comparisons are consistent
        idx = pd.to_datetime(s.index)
        if idx.tz is not None:
            idx = idx.tz_convert("UTC").tz_localize(None)
        now = pd.Timestamp.now()
        s = s[idx <= now]
        idx = idx[idx <= now]
        if len(s) > 0:
            # Shift back ~45 days: announcement date → approximate quarter end
            dates = idx - pd.Timedelta(days=45)
            ed_df = pd.DataFrame({"Date": dates, "EPS": s.values}).sort_values("Date")

    # ── Merge: qis is authoritative; ed fills in older quarters ─────────────
    if qis_df is not None and ed_df is not None and len(qis_df) < 12:
        cutoff = qis_df["Date"].min() - pd.Timedelta(days=30)
        older  = ed_df[ed_df["Date"] < cutoff]
        df = pd.concat([qis_df, older], ignore_index=True).sort_values("Date")
    elif qis_df is not None:
        df = qis_df
    elif ed_df is not None:
        df = ed_df
    else:
        return None

    if len(df) < 2:
        return None

    df = df.sort_values("Date").reset_index(drop=True)
    df["YoY (%)"] = yoy_pct(df["EPS"])
    df["Quarter"] = df["Date"].apply(qlabel)
    return df.tail(12).reset_index(drop=True)


def qchart(df, col, base_color, fmt_fn, height=275):
    """Bar chart with value labels. YoY % shown in title, not as a chart line."""
    bar_colors = []
    for yoy in df["YoY (%)"]:
        if pd.isna(yoy):       bar_colors.append(base_color)
        elif yoy >= 25:        bar_colors.append("#a6e3a1")
        elif yoy >= 0:         bar_colors.append(base_color)
        else:                  bar_colors.append("#f38ba8")

    fig = go.Figure()
    fig.add_bar(
        x=df["Quarter"], y=df[col],
        marker_color=bar_colors,
        text=[fmt_fn(v) for v in df[col]],
        textposition="outside", textfont=dict(size=11),
        showlegend=False,
    )
    fig.update_layout(
        xaxis=dict(type="category", tickfont=dict(size=10), tickangle=-35),
        yaxis=dict(tickfont=dict(size=10), showgrid=True, gridcolor="#2a2a3e"),
        plot_bgcolor="#1e1e2e", paper_bgcolor="#1e1e2e", font_color="#cdd6f4",
        margin=dict(l=35, r=20, t=8, b=5),
        height=height,
    )
    return fig


def yoy_label_list(df, col):
    """Return a compact inline YoY% string for each quarter, e.g. Q1'24: +12%  Q2'24: -3%"""
    rows = df.dropna(subset=["YoY (%)"])
    if rows.empty:
        return ""
    parts = []
    for _, r in rows.iterrows():
        sign = "+" if r["YoY (%)"] >= 0 else ""
        parts.append(f"{r['Quarter']}: {sign}{r['YoY (%)']:.0f}%")
    return " &nbsp;·&nbsp; ".join(parts)


@st.cache_data(ttl=3600, show_spinner=False)
def ai_overview(sym, name, industry, sector, desc):
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=550,
        messages=[{"role": "user", "content":
            f"Senior equity research analyst. ~220-word overview for {name} ({sym}).\n"
            f"Description: \"\"\"{(desc or '')[:1200]}\"\"\"\n"
            f"Sector: {sector} | Industry: {industry}\n\n"
            "Cover: 1) Business model & revenue drivers  "
            "2) Competitive position & moat  "
            "3) Management & capital allocation  "
            "4) 2-3 key risks.\n"
            "Prose only. No filler phrases."
        }],
    )
    return msg.content[0].text


# ─────────────────────────────────────────────────────────────────────────────
# App layout
# ─────────────────────────────────────────────────────────────────────────────

# ── Search ────────────────────────────────────────────────────────────────────
s1, s2, s3 = st.columns([1.8, 0.8, 8])
with s1:
    ticker_input = st.text_input("", value="AAPL", placeholder="Ticker…",
                                 label_visibility="collapsed")
with s2:
    st.button("Load", type="primary", use_container_width=True)

sym = ticker_input.strip().upper()
if not sym:
    st.stop()

with st.spinner(f"Loading {sym}…"):
    try:
        info, qis, ed, bs, cf, ih = load_data(sym)
    except RuntimeError as e:
        if "RATE_LIMIT" in str(e):
            st.warning("⚠️ Yahoo Finance is temporarily rate-limiting requests. Wait 30–60 seconds and try again.")
        else:
            st.error(f"Failed to load data: {e}")
        st.stop()
    except Exception as e:
        if "rate" in str(e).lower() or "429" in str(e):
            st.warning("⚠️ Yahoo Finance is temporarily rate-limiting requests. Wait 30–60 seconds and try again.")
        else:
            st.error(f"Failed to load data: {e}")
        st.stop()

if not info or info.get("quoteType") is None:
    st.error(f"No data for **{sym}**.")
    st.stop()

# ── Parse fields ──────────────────────────────────────────────────────────────
name     = safe_get(info, "longName") or safe_get(info, "shortName") or sym
sector   = safe_get(info, "sector",   "N/A")
industry = safe_get(info, "industry", "N/A")
desc     = safe_get(info, "longBusinessSummary", "")
exchange = safe_get(info, "exchange", "")

market_cap = safe_get(info, "marketCap")
pe_t       = safe_get(info, "trailingPE")
pe_f       = safe_get(info, "forwardPE")
peg        = safe_get(info, "pegRatio")
pb         = safe_get(info, "priceToBook")
ps         = safe_get(info, "priceToSalesTrailing12Months")
de         = safe_get(info, "debtToEquity")
roe_raw    = safe_get(info, "returnOnEquity")
roa_raw    = safe_get(info, "returnOnAssets")
gross_m    = safe_get(info, "grossMargins")
net_m      = safe_get(info, "profitMargins")
fcf        = safe_get(info, "freeCashflow")
eps_t      = safe_get(info, "trailingEps")
eps_f      = safe_get(info, "forwardEps")
div_raw    = safe_get(info, "dividendYield")
div_pct    = (div_raw if div_raw and div_raw > 1 else (div_raw * 100 if div_raw else None))
roe_pct    = roe_raw * 100 if roe_raw else None
roa_pct    = roa_raw * 100 if roa_raw else None

# ── Supplemental tile data ────────────────────────────────────────────────────
de_trend     = calc_de_trend(bs)
buyback_data = calc_buyback_yield(cf, market_cap)
insider_pct  = calc_insider_pct(info)
inst_signal  = calc_institutional_signal(ih, info)   # (trend, inst_pct, pos, neg)

# ── Quarterly data ────────────────────────────────────────────────────────────
# RS rating needs its own cache key — keep outside load_data to avoid slowing initial fetch

eps_df   = build_eps_df(qis, ed)
rev_df   = quarterly_df(qis, ["Total Revenue", "Revenue"], "Revenue")
qoq_accel = calc_qoq_accel(eps_df)

with st.spinner("Calculating RS rating…"):
    rs_data = calc_rs_rating(sym)   # cached; only slow on first run per ticker

# ── Company header ────────────────────────────────────────────────────────────
st.markdown(
    f"<span style='font-size:1.15rem;font-weight:700'>{name}</span> "
    f"<code style='font-size:0.85rem'>{sym}</code> "
    f"<span style='font-size:0.75rem;color:#a6adc8'>&nbsp;{exchange} · {sector} · {industry}</span>",
    unsafe_allow_html=True,
)
st.divider()

# ── Fundamentals strip ────────────────────────────────────────────────────────
st.markdown('<div class="sec">Fundamentals</div>', unsafe_allow_html=True)

# Row 1 — valuation
r1 = st.columns(10)
for col, (lbl, val) in zip(r1, [
    ("Mkt Cap",    fl(market_cap)),
    ("P/E (TTM)",  fv(pe_t, dec=1) if pe_t else "N/A"),
    ("P/E (Fwd)",  fv(pe_f, dec=1) if pe_f else "N/A"),
    ("PEG",        fv(peg,  dec=2) if peg  else "N/A"),
    ("P/Book",     fv(pb,   dec=2) if pb   else "N/A"),
    ("P/Sales",    fv(ps,   dec=2) if ps   else "N/A"),
    ("D/E",        fv(de,   dec=1) if de   else "N/A"),
    ("EPS (TTM)",  fv(eps_t, prefix="$", dec=2) if eps_t else "N/A"),
    ("EPS (Fwd)",  fv(eps_f, prefix="$", dec=2) if eps_f else "N/A"),
    ("Div Yield",  fv(div_pct, suffix="%") if div_pct else "N/A"),
]):
    col.metric(lbl, val)

# Row 2 — profitability + new tiles
r2 = st.columns(10)
# ── D/E Trend tile — embed direction in label so the delta number is never confusing
if de_trend:
    de_cur, de_chg, de_yrs, _ = de_trend
    arrow = "↓" if de_chg < -2 else ("↑" if de_chg > 2 else "→")
    de_lbl = f"D/E {arrow} ({de_yrs}Y: {de_chg:+.0f})"
    de_val = fv(de_cur, dec=1)
else:
    de_lbl, de_val = "D/E Trend", "N/A"

# ── Buybacks tile — show yield % and trend direction
bb_trend_icon = {"growing": "↑", "flat": "→", "declining": "↓",
                 "net_issuance": "⚠", "stopped": "✗"}.get(
                     buyback_data[1] if buyback_data else "", "")
bb_lbl = f"Buyback Yld {bb_trend_icon}" if buyback_data else "Buyback Yld"
bb_val = f"{buyback_data[0]:.2f}%" if buyback_data else "N/A"

# ── Insider ownership tile
insider_val   = fv(insider_pct, suffix="%", dec=2) if insider_pct is not None else "N/A"
insider_delta = round(insider_pct - 1.0, 2) if insider_pct is not None else None

# ── RS Rating tile
if rs_data:
    rs_rel, rs_stock, rs_sp = rs_data
    rs_val   = f"{rs_rel:+.1f}%"
    rs_delta = rs_rel          # positive = outperforming S&P = green
else:
    rs_val, rs_delta = "N/A", None

for col, (lbl, val, delta, dcolor) in zip(r2, [
    ("ROE",          fv(roe_pct, suffix="%") if roe_pct is not None else "N/A", None,         "off"),
    ("ROA",          fv(roa_pct, suffix="%") if roa_pct else "N/A",             None,         "off"),
    ("Gross Mgn",    fv(gross_m*100, suffix="%") if gross_m else "N/A",         None,         "off"),
    ("Net Mgn",      fv(net_m*100,   suffix="%") if net_m   else "N/A",         None,         "off"),
    ("FCF",          fl(fcf) if fcf else "N/A",                                 None,         "off"),
    (de_lbl,         de_val,                                                     None,         "off"),
    (bb_lbl,         bb_val,                                                     None,         "off"),
    ("Insider Own",  insider_val,                                                insider_delta,"normal"),
    ("RS vs S&P",    rs_val,                                                     rs_delta,     "normal"),
    ("", "",                                                                     None,         "off"),
]):
    if lbl:
        col.metric(lbl, val, delta=delta, delta_color=dcolor)

st.divider()

# ── CANSLIM signal bar ────────────────────────────────────────────────────────
st.markdown('<div class="sec">CANSLIM Signals</div>', unsafe_allow_html=True)

html = ""

# C — current quarterly EPS growth
if eps_df is not None:
    row = eps_df.dropna(subset=["YoY (%)"])
    if not row.empty:
        yoy = row.iloc[-1]["YoY (%)"]
        c = "g" if yoy >= 25 else ("y" if yoy >= 0 else "r")
        html += badge("C · EPS Qtr YoY", f"{yoy:+.0f}%", c)

# C — current quarterly revenue growth
if rev_df is not None:
    row = rev_df.dropna(subset=["YoY (%)"])
    if not row.empty:
        yoy = row.iloc[-1]["YoY (%)"]
        c = "g" if yoy >= 25 else ("y" if yoy >= 0 else "r")
        html += badge("C · Rev Qtr YoY", f"{yoy:+.0f}%", c)

# A — ROE (O'Neil target ≥ 17%)
if roe_pct is not None:
    c = "g" if roe_pct >= 17 else ("y" if roe_pct >= 10 else "r")
    html += badge("A · ROE", f"{roe_pct:.1f}%", c)

# A — YoY EPS acceleration (is the annual growth rate itself speeding up each quarter?)
if eps_df is not None:
    yoy_vals = eps_df.dropna(subset=["YoY (%)"]).tail(3)["YoY (%)"].tolist()
    if len(yoy_vals) == 3:
        if yoy_vals[2] > yoy_vals[1] > yoy_vals[0]:
            html += badge("A · YoY Accel", "Accelerating ↑", "g")
        elif yoy_vals[2] > yoy_vals[1]:
            html += badge("A · YoY Accel", "Improving →", "y")
        else:
            html += badge("A · YoY Accel", "Decelerating ↓", "r")

# A — Sequential QoQ EPS acceleration (are sequential growth rates trending up?)
if qoq_accel == "accelerating":
    html += badge("A · Q/Q EPS Trend", "Accelerating ↑", "g")
elif qoq_accel == "mixed":
    html += badge("A · Q/Q EPS Trend", "Mixed →", "y")
elif qoq_accel == "decelerating":
    html += badge("A · Q/Q EPS Trend", "Decelerating ↓", "r")

# L — Relative Strength vs S&P 500 (market leader check)
if rs_data:
    rs_rel, rs_stock, rs_sp = rs_data
    c = "g" if rs_rel >= 10 else ("y" if rs_rel >= -10 else "r")
    html += badge("L · RS vs S&P", f"{rs_rel:+.1f}% (stk {rs_stock:+.0f}% / mkt {rs_sp:+.0f}%)", c)

# N — PEG
if peg is not None:
    c = "g" if peg <= 1 else ("y" if peg <= 2 else "r")
    html += badge("Valuation · PEG", f"{peg:.2f}", c)

# Profitability guard
if roa_pct is not None:
    c = "g" if roa_pct >= 10 else ("y" if roa_pct >= 5 else "r")
    html += badge("ROA", f"{roa_pct:.1f}%", c)

# S — insider ownership
if insider_pct is not None:
    c = "g" if insider_pct >= 5 else ("y" if insider_pct >= 1 else "r")
    html += badge("S · Insider Own", f"{insider_pct:.2f}%", c)

# I — Institutional accumulation vs distribution
if inst_signal:
    i_trend, i_pct, i_pos, i_neg = inst_signal
    if i_pct and i_pct > 100:
        i_pct_str = f" ({i_pct:.1f}% held*)"   # >100% = float includes borrowed/short shares
    elif i_pct:
        i_pct_str = f" ({i_pct:.1f}% held)"
    else:
        i_pct_str = ""
    if i_trend == "accumulating":
        html += badge("I · Institutions", f"Accumulating ↑{i_pct_str}", "g")
    elif i_trend == "distributing":
        html += badge("I · Institutions", f"Distributing ↓{i_pct_str}", "r")
    elif i_trend in ("mixed", "insufficient"):
        html += badge("I · Institutions", f"Mixed →{i_pct_str}", "y")
    elif i_pct:
        html += badge("I · Institutions", f"{i_pct:.1f}% held", "y")

# Buybacks — yield + trend
if buyback_data:
    bb_yield, bb_trend, _ = buyback_data
    bb_colors = {"growing": "g", "flat": "y", "declining": "y",
                 "net_issuance": "r", "stopped": "r"}
    bb_icons  = {"growing": "↑", "flat": "→", "declining": "↓",
                 "net_issuance": "⚠", "stopped": "✗"}
    c = bb_colors.get(bb_trend, "y")
    icon = bb_icons.get(bb_trend, "")
    html += badge("Buybacks", f"{bb_yield:.2f}% yield {icon} ({bb_trend})", c)

# D/E trend signal
if de_trend:
    _, de_chg, de_yrs, _ = de_trend
    c = "g" if de_chg < -5 else ("r" if de_chg > 5 else "y")
    arrow = "↓ Reducing" if de_chg < -2 else ("↑ Rising" if de_chg > 2 else "→ Stable")
    html += badge(f"D/E Trend ({de_yrs}Y)", f"{arrow} ({de_chg:+.0f})", c)

st.markdown(html, unsafe_allow_html=True)
st.divider()

# ── Quarterly charts ──────────────────────────────────────────────────────────
ch_left, ch_right = st.columns(2)

with ch_left:
    eps_yoy_str = yoy_label_list(eps_df, "EPS") if eps_df is not None else ""
    st.markdown(
        f'<div class="sec">Quarterly EPS'
        f'{"&nbsp;&nbsp;<span style=\'color:#a6adc8;font-weight:400\'>YoY%: " + eps_yoy_str + "</span>" if eps_yoy_str else ""}'
        f'</div>',
        unsafe_allow_html=True,
    )
    if eps_df is not None and len(eps_df) >= 2:
        st.plotly_chart(
            qchart(eps_df, "EPS", "#89b4fa", lambda v: f"${v:.2f}" if pd.notna(v) else ""),
            use_container_width=True,
        )
    else:
        st.caption("Quarterly EPS data unavailable for this ticker.")

with ch_right:
    rev_yoy_str = yoy_label_list(rev_df, "Revenue") if rev_df is not None else ""
    st.markdown(
        f'<div class="sec">Quarterly Revenue'
        f'{"&nbsp;&nbsp;<span style=\'color:#a6adc8;font-weight:400\'>YoY%: " + rev_yoy_str + "</span>" if rev_yoy_str else ""}'
        f'</div>',
        unsafe_allow_html=True,
    )
    if rev_df is not None and len(rev_df) >= 2:
        st.plotly_chart(
            qchart(rev_df, "Revenue", "#89b4fa",
                   lambda v: f"${v/1e9:.1f}B" if abs(v) >= 1e9 else f"${v/1e6:.0f}M"),
            use_container_width=True,
        )
    else:
        st.caption("Quarterly revenue data unavailable for this ticker.")

# ── Company Overview (collapsed — doesn't add scroll height) ──────────────────
with st.expander("📋 Company Overview", expanded=False):
    if os.environ.get("ANTHROPIC_API_KEY"):
        with st.spinner("Generating…"):
            overview = ai_overview(sym, name, industry, sector, desc)
        if overview:
            st.markdown(overview)
        st.caption("Generated by Claude · Not investment advice")
    elif desc:
        st.write(desc)
