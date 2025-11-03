# app.py
"""
NSE Option Chain Dashboard (updated with default zoom, market-hours guard, better alerts)

1. Default chart view range selector (1 Day / 2 Days / 1 Week / 1 Month / All).
   Charts automatically zoom to that range on render.
2. Plotly modebar configured to include zoom2d / pan2d / autoScale2d etc so you can
   zoom by Y-axis (box zoom or pan).
3. Aggregated sample table now shows current fetched value AND timestamp.
4. Alerts play a short generated beep (synthesized at runtime) with cooldown.
5. The app will NOT append or save history if the current local time is outside market
   hours (09:15 — 15:30) or if today is Saturday/Sunday.
"""

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from nsepython import nse_optionchain_scrapper
from streamlit_autorefresh import st_autorefresh
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
import time
import json
import io
import wave
import struct
import math
import base64

# ---------- Config ----------
HISTORY_DIR = Path(".")
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

# Note: expanded mode bar tools to enable y-axis zoom/pan, autoscale, drawline, eraseshape.
PLOTLY_CONFIG = {
    "scrollZoom": True,
    "modeBarButtonsToAdd": [
        "zoom2d", "pan2d", "zoomIn2d", "zoomOut2d", "autoScale2d",
        "drawline", "drawopenpath", "drawrect", "drawcircle", "eraseshape"
    ],
    "displaylogo": False,
}

MARKET_OPEN_TIME = dt_time(9, 15)
MARKET_CLOSE_TIME = dt_time(15, 30)


# ---------- Helpers ----------
@st.cache_data(ttl=20)
def fetch_option_chain(symbol: str):
    """Fetch option chain JSON for a symbol using nsepython."""
    return nse_optionchain_scrapper(symbol)


def flatten_option_records(records):
    ce_rows = []
    pe_rows = []
    for r in records:
        if "CE" in r and r.get("CE"):
            ce_rows.append(r["CE"].copy())
        if "PE" in r and r.get("PE"):
            pe_rows.append(r["PE"].copy())
    df_ce = pd.DataFrame(ce_rows) if ce_rows else pd.DataFrame()
    df_pe = pd.DataFrame(pe_rows) if pe_rows else pd.DataFrame()

    numeric_cols = ["strikePrice", "lastPrice", "openInterest", "changeinOpenInterest", "totalTradedVolume"]
    for col in numeric_cols:
        if not df_ce.empty and col in df_ce.columns:
            df_ce[col] = pd.to_numeric(df_ce[col], errors="coerce")
        if not df_pe.empty and col in df_pe.columns:
            df_pe[col] = pd.to_numeric(df_pe[col], errors="coerce")

    if not df_ce.empty:
        df_ce = df_ce.dropna(subset=["strikePrice"])
    if not df_pe.empty:
        df_pe = df_pe.dropna(subset=["strikePrice"])

    return df_ce, df_pe


def safe_sum(df, col):
    if df is None or df.empty or col not in df.columns:
        return 0.0
    return float(pd.to_numeric(df[col].fillna(0), errors="coerce").sum())


def history_filename_csv(symbol: str) -> Path:
    safe_sym = str(symbol).replace("/", "_").replace("\\", "_").upper()
    return HISTORY_DIR / f"option_history_{safe_sym}.csv"


def save_history_csv(symbol: str) -> (bool, str):
    try:
        fname = history_filename_csv(symbol)
        hist = st.session_state.get("history", [])
        if not hist:
            pd.DataFrame(columns=["ts", "ce_ltp_sum", "pe_ltp_sum", "ce_oi_sum", "pe_oi_sum", "symbol", "expiry"]).to_csv(
                fname, index=False
            )
            return True, str(fname)
        df = pd.DataFrame(hist)
        df.to_csv(fname, index=False)
        return True, str(fname)
    except Exception as e:
        return False, str(e)


def load_history_csv(symbol: str, replace_existing=True) -> (bool, str):
    try:
        fname = history_filename_csv(symbol)
        if not fname.exists():
            return False, f"No saved history file: {fname}"
        df = pd.read_csv(fname)
        df["ts"] = df["ts"].astype(str)
        arr = df.to_dict(orient="records")
        if replace_existing:
            st.session_state["history"] = arr
        else:
            st.session_state["history"].extend(arr)
        st.session_state["loaded_symbol"] = symbol
        return True, str(fname)
    except Exception as e:
        return False, str(e)


def delete_history_csv(symbol: str) -> (bool, str):
    try:
        fname = history_filename_csv(symbol)
        if fname.exists():
            fname.unlink()
            return True, str(fname)
        return False, "File not found"
    except Exception as e:
        return False, str(e)


def is_market_open(now: datetime = None) -> bool:
    """Return True only when current local datetime is a weekday (Mon-Fri) and time inside market hours."""
    now = now or datetime.now()
    weekday = now.weekday()  # 0 = Mon, 6 = Sun
    if weekday >= 5:
        return False
    t = now.time()
    return (t >= MARKET_OPEN_TIME) and (t <= MARKET_CLOSE_TIME)


def generate_beep_datauri(freq=880.0, duration=0.18, sr=22050, amplitude=0.6):
    """Synthesize a short WAV beep and return a data:audio/wav;base64,... string."""
    n_samples = int(sr * duration)
    frames = bytearray()
    for i in range(n_samples):
        t = i / sr
        sample = amplitude * math.sin(2 * math.pi * freq * t)
        # mild fade-out to remove click
        fade = 1.0 - (i / n_samples) * 0.6
        val = int(sample * fade * 32767.0)
        frames += struct.pack("<h", val)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(frames)
    wav_bytes = buf.getvalue()
    b64 = base64.b64encode(wav_bytes).decode("ascii")
    return f"data:audio/wav;base64,{b64}"


def play_alert_sound(kind: str, cooldown=10.0):
    """Play a small generated beep sound (cooldown per kind)."""
    now_ts = time.time()
    key = f"last_alert_time_{kind}"
    last = st.session_state.get(key, 0.0)
    if now_ts - last < cooldown:
        return
    st.session_state[key] = now_ts
    data_uri = generate_beep_datauri()  # short generated beep (good small notification)
    html = f"""
    <audio autoplay>
      <source src="{data_uri}" type="audio/wav">
      Your browser does not support the audio element.
    </audio>
    <script>
      var a = document.querySelector('audio');
      try {{ a.play(); }} catch(e) {{ console.log('Autoplay blocked'); }}
    </script>
    """
    st.components.v1.html(html, height=0)


# ---------- Init ----------
if "history" not in st.session_state:
    st.session_state["history"] = []
if "shapes" not in st.session_state:
    st.session_state["shapes"] = []
if "loaded_symbol" not in st.session_state:
    st.session_state["loaded_symbol"] = None
for key in ["alert_triggered_ce", "alert_triggered_pe", "last_alert_time_ce", "last_alert_time_pe"]:
    if key not in st.session_state:
        st.session_state[key] = False if "alert_triggered" in key else 0.0

st.set_page_config(page_title="NSE Option Chain Dashboard", layout="wide")
st.title("📈 NSE Option Chain Dashboard")

# ---------- Sidebar ----------
with st.sidebar:
    st.header("Controls & Persistence")

    # symbol input - accept indices & stocks
    symbol = st.text_input("Symbol (index or stock) — e.g. NIFTY, BANKNIFTY, RELIANCE", value="NIFTY")
    symbol = str(symbol).strip().upper()

    st.markdown("---")
    st.markdown("Auto-refresh / Fetch")
    interval_label = st.selectbox("Auto-refresh interval", ["Off", "10 seconds", "20 seconds", "30 seconds", "1 minute"], index=2)
    interval_map_ms = {"Off": 0, "10 seconds": 10_000, "20 seconds": 20_000, "30 seconds": 30_000, "1 minute": 60_000}
    auto_interval_ms = interval_map_ms[interval_label]
    manual_fetch = st.button("Fetch Now", key="manual_fetch")

    st.markdown("---")
    st.markdown("Shapes & Export")
    shape_mode = st.selectbox("Add shape", ["None", "Horizontal Line", "Vertical Line", "Trend Line"], key="shape_mode")
    if shape_mode == "Horizontal Line":
        hl_price = st.number_input("Price (y)", value=100.0, key="hl_price")
        if st.button("Add Horizontal Line", key="add_hline_sidebar"):
            new_shape = {"type": "line", "y0": hl_price, "y1": hl_price, "x0": 0, "x1": 1, "xref": "paper", "yref": "y", "line": {"color": "rgba(255,0,0,0.7)", "width": 2, "dash": "dot"}}
            st.session_state["shapes"].append(new_shape)
            st.success(f"Added H-Line at {hl_price}")
    elif shape_mode == "Vertical Line":
        vl_strike = st.number_input("Strike (x)", value=20000.0, key="vl_strike")
        if st.button("Add Vertical Line", key="add_vline_sidebar"):
            new_shape = {"type": "line", "x0": vl_strike, "x1": vl_strike, "y0": 0, "y1": 1, "xref": "x", "yref": "paper", "line": {"color": "rgba(0,0,255,0.7)", "width": 2, "dash": "dot"}}
            st.session_state["shapes"].append(new_shape)
            st.success(f"Added V-Line at {vl_strike}")
    elif shape_mode == "Trend Line":
        t_x1 = st.number_input("x1 (strike)", value=20000.0, key="t_x1")
        t_y1 = st.number_input("y1 (price)", value=50.0, key="t_y1")
        t_x2 = st.number_input("x2 (strike)", value=21000.0, key="t_x2")
        t_y2 = st.number_input("y2 (price)", value=150.0, key="t_y2")
        if st.button("Add Trend Line", key="add_tline_sidebar"):
            new_shape = {"type": "line", "x0": t_x1, "y0": t_y1, "x1": t_x2, "y1": t_y2, "xref": "x", "yref": "y", "line": {"color": "rgba(0,255,0,0.7)", "width": 2, "dash": "dash"}}
            st.session_state["shapes"].append(new_shape)
            st.success("Added Trend Line")

    if st.button("Clear all shapes", key="clear_shapes_sidebar"):
        st.session_state["shapes"] = []
        st.info("All shapes cleared.")

    if st.button("Export shapes JSON", key="export_shapes_sidebar"):
        st.download_button("Download shapes.json", data=json.dumps(st.session_state["shapes"], indent=2), file_name="shapes.json", mime="application/json")

    st.markdown("---")
    st.markdown("Timeframe & Chart")
    timeframe_choice = st.selectbox("Aggregate timeframe", ["1 minute", "5 minutes", "10 minutes"], index=0)
    chart_type = st.selectbox("Chart type for CE & PE sums", ["Line", "Candlestick"], index=0)

    st.markdown("---")
    st.markdown("Default chart view range")
    default_view_range = st.selectbox(
        "Default view",
        ["1 Hour", "2 Hours", "4 Hours", "1 Day", "2 Days", "1 Week", "1 Month", "All"],
        index=3,  # default to "1 Day" (index may be adjusted)
    )


    st.markdown("---")
    st.subheader("Alerts (per-series)")
    ce_alert_enabled = st.toggle("Enable CE SUM alert", value=False, key="ce_alert_enabled")
    ce_alert_condition = st.selectbox("CE Condition", ["Greater Than", "Less Than"], index=0, key="ce_alert_cond")
    ce_alert_threshold = st.number_input("CE Threshold (SUM of CE LTP)", value=100000.0, min_value=0.0, step=1.0, key="ce_alert_thr")

    pe_alert_enabled = st.toggle("Enable PE SUM alert", value=False, key="pe_alert_enabled")
    pe_alert_condition = st.selectbox("PE Condition", ["Greater Than", "Less Than"], index=0, key="pe_alert_cond")
    pe_alert_threshold = st.number_input("PE Threshold (SUM of PE LTP)", value=100000.0, min_value=0.0, step=1.0, key="pe_alert_thr")

    st.markdown("---")
    st.subheader("History persistence (CSV)")
    if st.button("Load saved history for symbol", key="load_hist"):
        ok, info = load_history_csv(symbol, replace_existing=True)
        if ok:
            st.success(f"Loaded history from {info}")
        else:
            st.error(str(info))

    if st.button("Save current history now", key="save_hist"):
        ok, info = save_history_csv(symbol)
        if ok:
            st.success(f"Saved history to {info}")
        else:
            st.error(f"Save failed: {info}")

    if st.button("Clear saved history file", key="clear_saved_hist"):
        ok, info = delete_history_csv(symbol)
        if ok:
            st.success(f"Deleted saved history file: {info}")
        else:
            st.error(str(info))

    st.markdown("---")
    st.info("History is stored locally as CSV `option_history_{SYMBOL}.csv` in the app working directory.")

# ---------- Autorefresh ----------
if auto_interval_ms > 0:
    st_autorefresh(interval=auto_interval_ms, limit=None, key="autorefresh_csv")

# Auto-load saved history on symbol change
# Auto-load saved history on symbol change (fixed: do NOT keep previous symbol's history if no file exists)
if st.session_state.get("loaded_symbol") != symbol:
    ok, info = load_history_csv(symbol, replace_existing=True)
    if ok:
        st.sidebar.success(f"Auto-loaded saved history for {symbol}")
        st.session_state["loaded_symbol"] = symbol
    else:
        # If there is no saved CSV for the new symbol, clear any previous in-memory history
        # so charts won't show the last symbol's data mistakenly.
        st.session_state["history"] = []
        st.session_state["loaded_symbol"] = symbol
        # Only show a sidebar message if the failure wasn't just "file missing"
        if not str(info).startswith("No saved history file"):
            st.sidebar.warning(f"Failed to load saved history: {info}")


# ---------- Main layout ----------
col_left, col_right = st.columns([2, 1])
with col_left:
    st.header("Option Chain Viewer")
    expiry_override = st.checkbox("Choose expiry manually", value=False, key="expiry_override_main")
    selected_expiry = None
with col_right:
    show_only_oi_changes = st.checkbox("Show only strikes with OI change", value=False)

# placeholders / metrics
metric_col1, metric_col2, metric_col3 = st.columns(3)
placeholder_symbol = metric_col1.empty()
placeholder_underlying = metric_col2.empty()
placeholder_fetchinfo = metric_col3.empty()

# bigger font display for CE/PE sum
big_col1, big_col2 = st.columns([1, 1])
big_ce = big_col1.empty()
big_pe = big_col2.empty()

# other placeholders
stat_col1, stat_col2, stat_col3 = st.columns(3)
placeholder_ce_oi_pct = stat_col1.empty()
placeholder_pe_oi_pct = stat_col2.empty()
placeholder_suminfo = stat_col3.empty()

# defaults
df_ce, df_pe = pd.DataFrame(), pd.DataFrame()
underlying_value = None
ce_sum = pe_sum = ce_oi_sum = pe_oi_sum = 0.0

# Fetch logic
should_fetch = manual_fetch or (auto_interval_ms > 0) or (st.session_state.get("last_fetch") is None)

# Decide if market is open now
market_open_now = is_market_open()

if should_fetch:
    try:
        with st.spinner(f"Fetching option chain for {symbol}..."):
            data = fetch_option_chain(symbol)

        records = data.get("records", {}).get("data", [])
        expiries = data.get("records", {}).get("expiryDates", []) or []
        underlying_value = data.get("records", {}).get("underlyingValue")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st.session_state["last_fetch"] = timestamp
        st.session_state["fetch_count"] = st.session_state.get("fetch_count", 0) + 1

        if expiries:
            if expiry_override:
                selected_expiry = st.selectbox("Select expiry", expiries, key="expiry_select_main")
            else:
                selected_expiry = expiries[0]

        records_filtered = [r for r in records if (not selected_expiry) or r.get("expiryDate") == selected_expiry] if records else []
        df_ce, df_pe = flatten_option_records(records_filtered)

        ce_sum = safe_sum(df_ce, "lastPrice")
        pe_sum = safe_sum(df_pe, "lastPrice")
        ce_oi_sum = safe_sum(df_ce, "openInterest")
        pe_oi_sum = safe_sum(df_pe, "openInterest")

        history_entry = {
            "ts": timestamp,
            "ce_ltp_sum": ce_sum,
            "pe_ltp_sum": pe_sum,
            "ce_oi_sum": ce_oi_sum,
            "pe_oi_sum": pe_oi_sum,
            "symbol": symbol,
            "expiry": selected_expiry,
        }

        # Only append & save if market is open and not weekend
       if market_open_now:
            st.session_state["history"].append(history_entry)
            ok, info = save_history_csv(symbol)
            if not ok:
                st.warning(f"Auto-save failed: {info}")

            # --- NEW: store to Supabase (upload JSON + insert DB row) ---
            try:
                # payload we upload can be the same history_entry or the full 'data' response
                payload_to_store = {
                    "history_entry": history_entry,
                    "underlying_value": underlying_value,
                    "raw_records_count": len(records) if records is not None else 0
                }

                # pass the owner field (no auth required in this quick setup)
                # Make sure `store_fetch_record` supports an `owner` argument (see step 3 below)
                res = store_fetch_record(
                    payload=payload_to_store,
                    symbol=symbol,
                    expiry=selected_expiry,
                    save_file=True,
                    owner=owner_name
                )

                # handle response shapes from supabase client
                data = getattr(res, "data", None)
                error = getattr(res, "error", None) or (res.get("error") if isinstance(res, dict) else None)

                if error:
                    st.sidebar.warning(f"Supabase insert error: {error}")
                else:
                    # show confirmation but keep it subtle (no spam on every auto-fetch)
                    if st.session_state.get("fetch_count", 0) % 5 == 0 or manual_fetch:
                        st.sidebar.success("Stored latest fetch to Supabase")
            except Exception as e:
                st.sidebar.warning(f"Failed to store to Supabase: {e}")
        else:
            st.info("Market is closed (outside 09:15-15:30 or weekend) — not appending history or saving to CSV.")

# Update placeholders
placeholder_symbol.markdown(f"**Symbol:** `{symbol or '-'}`")
placeholder_underlying.markdown(f"**Underlying:** `{underlying_value if underlying_value is not None else '-'}`")
placeholder_fetchinfo.markdown(
    f"**Last fetch:** `{st.session_state.get('last_fetch', 'N/A')}`<br>**Fetch count:** `{st.session_state.get('fetch_count', 0)}`",
    unsafe_allow_html=True,
)

# show bigger CE/PE sums using HTML (increased font)
big_ce.markdown(f"<div style='font-size:34px; font-weight:600'>CE SUM (latest): {ce_sum:,.0f}</div>", unsafe_allow_html=True)
big_pe.markdown(f"<div style='font-size:34px; font-weight:600'>PE SUM (latest): {pe_sum:,.0f}</div>", unsafe_allow_html=True)

# Compute CE/PE OI % change from last two history points
ce_oi_change_pct = pe_oi_change_pct = None
hist_local = st.session_state.get("history", [])
if len(hist_local) >= 2:
    prev = hist_local[-2]
    curr = hist_local[-1]
    prev_ce = float(prev.get("ce_oi_sum", 0.0))
    prev_pe = float(prev.get("pe_oi_sum", 0.0))
    curr_ce = float(curr.get("ce_oi_sum", 0.0))
    curr_pe = float(curr.get("pe_oi_sum", 0.0))
    if prev_ce != 0:
        ce_oi_change_pct = ((curr_ce - prev_ce) / prev_ce) * 100.0
    else:
        ce_oi_change_pct = None
    if prev_pe != 0:
        pe_oi_change_pct = ((curr_pe - prev_pe) / prev_pe) * 100.0
    else:
        pe_oi_change_pct = None

# Display OI % change metrics
if ce_oi_change_pct is None:
    placeholder_ce_oi_pct.metric("CE OI % Change (last interval)", "N/A")
else:
    placeholder_ce_oi_pct.metric("CE OI % Change (last interval)", f"{ce_oi_change_pct:.2f}%", delta=f"{ce_oi_change_pct:.2f}%")

if pe_oi_change_pct is None:
    placeholder_pe_oi_pct.metric("PE OI % Change (last interval)", "N/A")
else:
    placeholder_pe_oi_pct.metric("PE OI % Change (last interval)", f"{pe_oi_change_pct:.2f}%", delta=f"{pe_oi_change_pct:.2f}%")

placeholder_suminfo.info("OI DATA")

st.write(f"**Total CE Open Interest:** {ce_oi_sum:,.0f} | **Total PE Open Interest:** {pe_oi_sum:,.0f}")
st.markdown("---")

# Show CE & PE DataFrames
df_col1, df_col2 = st.columns(2)
with df_col1:
    st.markdown(f"#### Calls (CE) for {selected_expiry or 'default expiry'}")
    if not df_ce.empty:
        df_ce_display = df_ce[df_ce["changeinOpenInterest"].fillna(0) != 0] if show_only_oi_changes else df_ce
        st.dataframe(df_ce_display.reset_index(drop=True), height=240)
    else:
        st.info("No CE data available for the selected expiry / symbol.")

with df_col2:
    st.markdown(f"#### Puts (PE) for {selected_expiry or 'default expiry'}")
    if not df_pe.empty:
        df_pe_display = df_pe[df_pe["changeinOpenInterest"].fillna(0) != 0] if show_only_oi_changes else df_pe
        st.dataframe(df_pe_display.reset_index(drop=True), height=240)
    else:
        st.info("No PE data available for the selected expiry / symbol.")

# OI Bar charts (per-strike)
st.markdown("### Open Interest Charts (per strike)")
chart_col1, chart_col2 = st.columns(2)


def create_oi_chart(df, title):
    if df.empty:
        return go.Figure()
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df["strikePrice"], y=df["openInterest"], name="Total OI"))
    if "changeinOpenInterest" in df.columns:
        fig.add_trace(go.Bar(x=df["strikePrice"], y=df["changeinOpenInterest"], name="Change in OI", opacity=0.6))
    total_oi = float(df["openInterest"].sum()) if "openInterest" in df.columns else 0.0
    fig.update_layout(
        title=title,
        barmode="overlay",
        xaxis_title="Strike Price",
        yaxis_title="Open Interest",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        annotations=[dict(x=0.05, y=0.95, xref="paper", yref="paper", text=f"Total OI: {total_oi:,.0f}", showarrow=False)],
        dragmode="zoom",
    )
    return fig


with chart_col1:
    st.subheader("Calls (CE) Open Interest")
    if not df_ce.empty:
        fig_ce_oi = create_oi_chart(df_ce, "CE Open Interest by Strike")
        fig_ce_oi.update_layout(shapes=st.session_state["shapes"])
        st.plotly_chart(fig_ce_oi, use_container_width=True, config=PLOTLY_CONFIG)
    else:
        st.info("No CE data available to display chart.")

with chart_col2:
    st.subheader("Puts (PE) Open Interest")
    if not df_pe.empty:
        fig_pe_oi = create_oi_chart(df_pe, "PE Open Interest by Strike")
        fig_pe_oi.update_layout(shapes=st.session_state["shapes"])
        st.plotly_chart(fig_pe_oi, use_container_width=True, config=PLOTLY_CONFIG)
    else:
        st.info("No PE data available to display chart.")

st.markdown("---")

# ---------- Timeframe aggregation & charts: CE sum and PE sum individually ----------
st.markdown("### CE SUM and PE SUM — Timeframe analysis")

history_df = pd.DataFrame(st.session_state.get("history", []))
if not history_df.empty:
    history_df["ts_dt"] = pd.to_datetime(history_df["ts"])
    history_df = history_df.set_index("ts_dt").sort_index()

    timeframe_map = {"1 minute": "1min", "5 minutes": "5min", "10 minutes": "10min"}
    freq = timeframe_map.get(timeframe_choice, "1min")

    resampled_ce = history_df["ce_ltp_sum"].resample(freq)
    resampled_pe = history_df["pe_ltp_sum"].resample(freq)

    ce_line = resampled_ce.last().dropna()
    pe_line = resampled_pe.last().dropna()

    ce_ohlc = resampled_ce.ohlc().dropna() if not resampled_ce.agg(len).empty else pd.DataFrame()
    pe_ohlc = resampled_pe.ohlc().dropna() if not resampled_pe.agg(len).empty else pd.DataFrame()

    # map default_view_range to datetime range for automatic zoom
    now_idx = history_df.index.max()
    end_dt = now_idx if now_idx is not pd.NaT else datetime.now()
    start_dt = None
    if default_view_range == "1 Hour":
        start_dt = end_dt - timedelta(hours=1)
    elif default_view_range == "2 Hours":
        start_dt = end_dt - timedelta(hours=2)
    elif default_view_range == "4 Hours":
        start_dt = end_dt - timedelta(hours=4)
    elif default_view_range == "1 Day":
        start_dt = end_dt - timedelta(days=1)
    elif default_view_range == "2 Days":
        start_dt = end_dt - timedelta(days=2)
    elif default_view_range == "1 Week":
        start_dt = end_dt - timedelta(days=7)
    elif default_view_range == "1 Month":
        start_dt = end_dt - timedelta(days=30)
    elif default_view_range == "All":
        start_dt = history_df.index.min()


    plot_col1, plot_col2 = st.columns([1, 1])
    # CE chart with current value line + annotation
    with plot_col1:
        st.subheader("CE SUM LTP")
        ce_shapes = list(st.session_state["shapes"])
        if ce_sum is not None and ce_sum != 0:
            x0 = ce_line.index[0] if not ce_line.empty else history_df.index.min()
            x1 = ce_line.index[-1] if not ce_line.empty else history_df.index.max()
            ce_current_shape = {"type": "line", "xref": "x", "yref": "y", "x0": x0, "x1": x1, "y0": ce_sum, "y1": ce_sum, "line": {"color": "royalblue", "width": 2, "dash": "dashdot"}}
            ce_shapes.append(ce_current_shape)

        if chart_type == "Line":
            if not ce_line.empty:
                fig_ce_line = go.Figure()
                fig_ce_line.add_trace(go.Scatter(x=ce_line.index, y=ce_line.values, mode="lines+markers", name="CE SUM LTP"))
                if ce_sum is not None:
                    fig_ce_line.add_annotation(x=ce_line.index[-1], y=ce_sum, text=f"Current: {ce_sum:,.0f} @ {st.session_state.get('last_fetch','-')}", showarrow=True, arrowhead=2, ax=40, ay=-30, bgcolor="rgba(255,255,255,0.8)")
                fig_ce_line.update_layout(title=f"CE SUM LTP — {timeframe_choice}", xaxis_title="Time", yaxis_title="SUM LTP", shapes=ce_shapes, dragmode="zoom")
                # set default x-range if available
                if start_dt is not None:
                    fig_ce_line.update_xaxes(range=[start_dt.isoformat(), end_dt.isoformat()])
                # ensure y-axis zoom / pan allowed
                fig_ce_line.update_layout(yaxis=dict(fixedrange=False))
                st.plotly_chart(fig_ce_line, use_container_width=True, config=PLOTLY_CONFIG)
            else:
                st.info("Not enough CE aggregated history to plot line.")
        else:
            if not ce_ohlc.empty:
                fig_ce_candle = go.Figure(data=[go.Candlestick(x=ce_ohlc.index, open=ce_ohlc["open"], high=ce_ohlc["high"], low=ce_ohlc["low"], close=ce_ohlc["close"])])
                if ce_sum is not None:
                    fig_ce_candle.add_hline(y=ce_sum, line_dash="dashdot", line_color="royalblue")
                    fig_ce_candle.add_annotation(x=ce_ohlc.index[-1], y=ce_sum, text=f"Current: {ce_sum:,.0f} @ {st.session_state.get('last_fetch','-')}", showarrow=True, arrowhead=2, ax=40, ay=-30, bgcolor="rgba(255,255,255,0.8)")
                fig_ce_candle.update_layout(title=f"CE SUM OHLC — {timeframe_choice}", shapes=st.session_state["shapes"], dragmode="zoom")
                if start_dt is not None:
                    fig_ce_candle.update_xaxes(range=[start_dt.isoformat(), end_dt.isoformat()])
                fig_ce_candle.update_layout(yaxis=dict(fixedrange=False))
                st.plotly_chart(fig_ce_candle, use_container_width=True, config=PLOTLY_CONFIG)
            else:
                st.info("Not enough CE aggregated history to plot candlestick.")

    # PE chart with current value
    with plot_col2:
        st.subheader("PE SUM LTP")
        pe_shapes = list(st.session_state["shapes"])
        if pe_sum is not None and pe_sum != 0:
            x0 = pe_line.index[0] if not pe_line.empty else history_df.index.min()
            x1 = pe_line.index[-1] if not pe_line.empty else history_df.index.max()
            pe_current_shape = {"type": "line", "xref": "x", "yref": "y", "x0": x0, "x1": x1, "y0": pe_sum, "y1": pe_sum, "line": {"color": "firebrick", "width": 2, "dash": "dashdot"}}
            pe_shapes.append(pe_current_shape)

        if chart_type == "Line":
            if not pe_line.empty:
                fig_pe_line = go.Figure()
                fig_pe_line.add_trace(go.Scatter(x=pe_line.index, y=pe_line.values, mode="lines+markers", name="PE SUM LTP"))
                if pe_sum is not None:
                    fig_pe_line.add_annotation(x=pe_line.index[-1], y=pe_sum, text=f"Current: {pe_sum:,.0f} @ {st.session_state.get('last_fetch','-')}", showarrow=True, arrowhead=2, ax=40, ay=-30, bgcolor="rgba(255,255,255,0.8)")
                fig_pe_line.update_layout(title=f"PE SUM LTP — {timeframe_choice}", xaxis_title="Time", yaxis_title="SUM LTP", shapes=pe_shapes, dragmode="zoom")
                if start_dt is not None:
                    fig_pe_line.update_xaxes(range=[start_dt.isoformat(), end_dt.isoformat()])
                fig_pe_line.update_layout(yaxis=dict(fixedrange=False))
                st.plotly_chart(fig_pe_line, use_container_width=True, config=PLOTLY_CONFIG)
            else:
                st.info("Not enough PE aggregated history to plot line.")
        else:
            if not pe_ohlc.empty:
                fig_pe_candle = go.Figure(data=[go.Candlestick(x=pe_ohlc.index, open=pe_ohlc["open"], high=pe_ohlc["high"], low=pe_ohlc["low"], close=pe_ohlc["close"])])
                if pe_sum is not None:
                    fig_pe_candle.add_hline(y=pe_sum, line_dash="dashdot", line_color="firebrick")
                    fig_pe_candle.add_annotation(x=pe_ohlc.index[-1], y=pe_sum, text=f"Current: {pe_sum:,.0f} @ {st.session_state.get('last_fetch','-')}", showarrow=True, arrowhead=2, ax=40, ay=-30, bgcolor="rgba(255,255,255,0.8)")
                fig_pe_candle.update_layout(title=f"PE SUM OHLC — {timeframe_choice}", shapes=st.session_state["shapes"], dragmode="zoom")
                if start_dt is not None:
                    fig_pe_candle.update_xaxes(range=[start_dt.isoformat(), end_dt.isoformat()])
                fig_pe_candle.update_layout(yaxis=dict(fixedrange=False))
                st.plotly_chart(fig_pe_candle, use_container_width=True, config=PLOTLY_CONFIG)
            else:
                st.info("Not enough PE aggregated history to plot candlestick.")

    # small aggregated sample now shows current fetched value AND time at top
    st.markdown("#### Aggregated (sum current)")
    sample_rows = []
    # get tail values for CE and PE
    ce_tail = list(ce_line.tail(10).items()) if not ce_line.empty else []
    pe_tail = list(pe_line.tail(10).items()) if not pe_line.empty else []

    # Build merged sample rows by time - simpler: just show CE tail and PE tail side by side where possible
    max_len = max(len(ce_tail), len(pe_tail))
    for i in range(max_len):
        ce_time, ce_val = ce_tail[-(i+1)] if i < len(ce_tail) else (None, None)
        pe_time, pe_val = pe_tail[-(i+1)] if i < len(pe_tail) else (None, None)
        sample_rows.append({"time": ce_time or pe_time, "CE_last": ce_val, "PE_last": pe_val})

    # Prepend current row with timestamp
    current_row = {"time": st.session_state.get("last_fetch", datetime.now().strftime("%Y-%m-%d %H:%M:%S")), "CE_last": ce_sum, "PE_last": pe_sum}
    sample_df = pd.DataFrame([current_row] + sample_rows)
    if not sample_df.empty:
        # format time column as string
        sample_df["time"] = sample_df["time"].astype(str)
        st.dataframe(sample_df.fillna("").head(20), height=240)

    # download full saved history
    csv_bytes = history_df.reset_index().to_csv(index=False).encode("utf-8")
    st.download_button("Download full saved history (CSV)", data=csv_bytes, file_name=history_filename_csv(symbol).name, mime="text/csv")

else:
    st.info("No historical data yet. It will populate after you fetch / auto-refresh. Use 'Load saved history' to load previous CSV for this symbol if present.")

st.markdown("---")

# ---------- Alerts per-series ----------
if ce_alert_enabled and ce_sum is not None:
    cond_met_ce = (ce_alert_condition == "Greater Than" and ce_sum > ce_alert_threshold) or (ce_alert_condition == "Less Than" and ce_sum < ce_alert_threshold)
    if cond_met_ce:
        st.warning(f"🔔 CE ALERT: CE SUM = {ce_sum:.2f} is {ce_alert_condition.lower()} {ce_alert_threshold:.2f}")
        if not st.session_state.get("alert_triggered_ce", False):
            play_alert_sound("ce")
            st.session_state["alert_triggered_ce"] = True
    else:
        st.session_state["alert_triggered_ce"] = False

if pe_alert_enabled and pe_sum is not None:
    cond_met_pe = (pe_alert_condition == "Greater Than" and pe_sum > pe_alert_threshold) or (pe_alert_condition == "Less Than" and pe_sum < pe_alert_threshold)
    if cond_met_pe:
        st.warning(f"🔔 PE ALERT: PE SUM = {pe_sum:.2f} is {pe_alert_condition.lower()} {pe_alert_threshold:.2f}")
        if not st.session_state.get("alert_triggered_pe", False):
            play_alert_sound("pe")
            st.session_state["alert_triggered_pe"] = True
    else:
        st.session_state["alert_triggered_pe"] = False

# ---------- Session History Charts ----------
st.markdown("### Session History (CE vs PE)")

if not history_df.empty:
    hist_col1, hist_col2 = st.columns(2)
    with hist_col1:
        fig_hist_ltp = px.line(history_df.reset_index(), x="ts_dt", y=["ce_ltp_sum", "pe_ltp_sum"], labels={"value": "LTP Sum", "ts_dt": "Time", "variable": "Series"}, title=f"CE vs PE LTP Sum Over Time ({symbol})")
        fig_hist_ltp.update_layout(dragmode="zoom")
        # apply default view range to history chart as well
        if start_dt is not None:
            fig_hist_ltp.update_xaxes(range=[start_dt.isoformat(), end_dt.isoformat()])
        st.plotly_chart(fig_hist_ltp, use_container_width=True, config=PLOTLY_CONFIG)
    with hist_col2:
        fig_hist_oi = px.line(history_df.reset_index(), x="ts_dt", y=["ce_oi_sum", "pe_oi_sum"], labels={"value": "OI Sum", "ts_dt": "Time", "variable": "Series"}, title=f"CE vs PE OI Sum Over Time ({symbol})")
        fig_hist_oi.update_layout(dragmode="zoom")
        if start_dt is not None:
            fig_hist_oi.update_xaxes(range=[start_dt.isoformat(), end_dt.isoformat()])
        st.plotly_chart(fig_hist_oi, use_container_width=True, config=PLOTLY_CONFIG)
else:
    st.info("No session history to show yet.")

st.markdown("---")
st.caption("History auto-saved per-symbol as CSV during market hours only. Charts are zoomable; use the modebar to draw/analyze. Current fetched CE/PE values and timestamp are shown in the aggregated sample.")

