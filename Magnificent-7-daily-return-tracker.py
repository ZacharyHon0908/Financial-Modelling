
import subprocess, sys

# -----------------------------------------------------------------------------
# STEP 0: Auto-install
# -----------------------------------------------------------------------------
def _install_if_missing(packages: list[str]):
    for pkg in packages:
        try:
            __import__(pkg)
        except ImportError:
            print(f"[Setup] '{pkg}' not found -> installing ...")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pkg],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            print(f"[Setup] '{pkg}' installed OK")

_install_if_missing(["yfinance", "pandas"])

# -----------------------------------------------------------------------------
# STEP 1: Imports
# -----------------------------------------------------------------------------
import math
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.filedialog import asksaveasfilename
from datetime import date, timedelta, datetime

import pandas as pd
import yfinance as yf

# -----------------------------------------------------------------------------
# STEP 2: Config
# -----------------------------------------------------------------------------
M7: dict[str, str] = {
    "AAPL":  "Apple",
    "MSFT":  "Microsoft",
    "GOOGL": "Alphabet",
    "AMZN":  "Amazon",
    "NVDA":  "NVIDIA",
    "META":  "Meta",
    "TSLA":  "Tesla",
}
TICKERS = list(M7.keys())

LOOKBACK_YEARS = 5


def current_window() -> tuple[date, date]:
    """
    Recomputed on every refresh. Freezing these at import meant an app left
    open past midnight kept asking for yesterday's window forever, while
    five_yr_start elsewhere moved with the clock.
    """
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=LOOKBACK_YEARS * 365 + 5)
    return start, end


# --- validation thresholds ---------------------------------------------------
# No mega-cap has ever moved this much in one session; above this it is a data
# or corporate-action problem, not a market move.
MAX_PLAUSIBLE_MOVE_PCT = 40.0

# GUARD 2. On a zero-dividend day the two return definitions are algebraically
# identical, so any gap beyond numerical noise means the split basis is wrong.
# A real split-basis error is off by the split RATIO: hundreds of percentage
# points (10:1 produces ~909pp). The measured noise floor, set by float
# precision and the finite precision of Yahoo's Adj Close, is around 1e-4pp.
# 0.01pp sits ~100x above the noise and ~90,000x below the smallest real signal.
ADJ_CLOSE_TOLERANCE_PCT = 0.01

# GUARD 3. Ex-dividend days, expressed as a fraction of the prior close so it
# is comparable to a return. Looser than GUARD 2 on purpose: Yahoo's own
# adjusted close is known to handle pre-split dividends inconsistently, so a
# hit here may be the vendor's problem rather than this code's. Treat it as
# "look at this", not "this is broken".
DIV_BASIS_TOLERANCE_PCT = 0.05

# Visual constants
ROW_H      = 22
HDR_H      = 28
COL_WIDTHS = [108] + [132] * len(TICKERS)
TOTAL_W    = sum(COL_WIDTHS)

COL_POS    = "#c6efce"
COL_NEG    = "#ffc7ce"
COL_FLAT   = "#ffffff"
COL_DATE   = "#dde3f0"
COL_HDR_BG = "#0d1b2a"
COL_HDR_FG = "white"
COL_GRID   = "#cccccc"

FONT_HDR  = ("Helvetica",   10, "bold")
FONT_CELL = ("Courier New", 10)
FONT_DATE = ("Helvetica",   10)


# -----------------------------------------------------------------------------
# STEP 3: Timezone helper
# -----------------------------------------------------------------------------
def to_naive(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    return idx.normalize()


# -----------------------------------------------------------------------------
# STEP 4: Corporate actions, data fetch, return computation
# -----------------------------------------------------------------------------
def normalise_dividends_to_split_basis(dividends: pd.Series,
                                       splits: pd.Series) -> pd.Series:
    """
    Put RAW dividends onto the same split basis as Yahoo's already-adjusted
    Close: divide each dividend by the cumulative ratio of every split that
    happened AFTER it.

    Deliberately NOT applied to Close, which Yahoo already split-adjusts.

    FIX 1: the old guard was `if ratio is None or ratio <= 0`. NaN <= 0 is
    False, so a NaN ratio slipped through and turned every earlier dividend
    into NaN, which then propagated into the returns with no warning.
    `not (ratio > 0)` is False for NaN too, so NaN, 0 and negatives are all
    skipped.
    """
    d = dividends.copy()
    for split_dt, ratio in splits.sort_index().items():
        if ratio is None or not (ratio > 0):
            print(f"     ignoring unusable split ratio {ratio!r} on "
                  f"{getattr(split_dt, 'date', lambda: split_dt)()}")
            continue
        d.loc[d.index < split_dt] /= ratio
    return d


def fetch_symbol(symbol: str, start: date, end: date):
    """Return (close, dividends_on_split_basis, adj_close, splits_applied)."""
    ticker_obj = yf.Ticker(symbol)
    hist = ticker_obj.history(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=False,      # keep raw Close AND Adj Close for cross-checks
        actions=True,
    )
    if hist.empty:
        empty = pd.Series(dtype=float, name=symbol)
        return empty, empty, empty, pd.Series(dtype=float)

    hist.index = to_naive(hist.index)

    close = hist["Close"].astype(float).copy()          # already split-adjusted
    div   = hist["Dividends"].astype(float).copy()      # raw, needs normalising
    adj   = (hist["Adj Close"].astype(float).copy()
             if "Adj Close" in hist.columns else pd.Series(dtype=float))

    # FIX 2: bound the split window at BOTH ends. With only a lower bound, a
    # split dated after the data window (an announced-but-not-yet-effective
    # split, or bad vendor data) divided every dividend in the window by that
    # ratio. Measured impact in the harness: all 1,305 rows shifted, worst
    # single day off by 10.19pp, and nothing warned.
    splits = ticker_obj.splits
    splits_applied = pd.Series(dtype=float)
    if splits is not None and not splits.empty:
        splits = splits.copy()
        splits.index = to_naive(splits.index)
        in_window = ((splits.index >= pd.Timestamp(start)) &
                     (splits.index <= pd.Timestamp(end)))
        splits_applied = splits[in_window]
        ignored = splits[~in_window]
        for split_dt, ratio in splits_applied.sort_index().items():
            print(f"  [{symbol}] split {ratio:g}:1 on {split_dt.date()} "
                  f"-> dividends before this date divided by {ratio:g}")
        for split_dt, ratio in ignored.sort_index().items():
            print(f"  [{symbol}] split {ratio:g}:1 on {split_dt.date()} is "
                  f"outside the data window, ignored")

    if not splits_applied.empty:
        div = normalise_dividends_to_split_basis(div, splits_applied)

    # No rounding here: rounding before the reconciliation raises the noise
    # floor. Rounding belongs in the display layer.
    for s in (close, div, adj):
        s.name = symbol
    return close, div, adj, splits_applied


def compute_daily_returns():
    """Return (returns_pct_dataframe, diagnostics, (start, end))."""
    start, end = current_window()
    print(f"  Window: {start} -> {end}")

    returns_dict: dict[str, pd.Series] = {}
    diagnostics: list[str] = []

    for symbol in TICKERS:
        print(f"  Processing {symbol} ({M7[symbol]}) ...")
        close, div, adj, _ = fetch_symbol(symbol, start, end)

        if close.empty:
            returns_dict[symbol] = pd.Series(dtype=float, name=symbol)
            diagnostics.append(f"{symbol}: no data returned")
            continue

        ret = (close + div - close.shift(1)) / close.shift(1) * 100
        ret.name = symbol

        # --- GUARD 0: silent NaN detection -----------------------------------
        # Cheap, and it is what would have caught the NaN-split-ratio bug.
        nan_ret = int(ret.iloc[1:].isna().sum())
        if nan_ret:
            diagnostics.append(
                f"{symbol}: {nan_ret} of {len(ret) - 1} returns are NaN "
                f"(missing prices or an unusable corporate action)")

        # --- GUARD 1: plausibility -------------------------------------------
        for dt, val in ret[ret.abs() > MAX_PLAUSIBLE_MOVE_PCT].dropna().items():
            diagnostics.append(
                f"{symbol}: implausible {val:+.2f}% on {dt.date()} "
                f"(check corporate actions)")

        if not adj.empty:
            ret_adj = (adj / adj.shift(1) - 1) * 100

            # --- GUARD 2: zero-dividend days vs Adj Close --------------------
            no_div = (div == 0) & ret.notna() & ret_adj.notna()
            gap = (ret[no_div] - ret_adj[no_div]).abs()
            if not gap.empty:
                worst_dt, worst = gap.idxmax(), gap.max()
                print(f"  [{symbol}] split-basis check: max gap {worst:.2e}pp "
                      f"on {worst_dt.date()} ({len(gap)} zero-dividend days)")
                if worst > ADJ_CLOSE_TOLERANCE_PCT:
                    diagnostics.append(
                        f"{symbol}: split basis mismatch, {worst:.4f}pp gap vs "
                        f"Adj Close on {worst_dt.date()}")

            # --- GUARD 3: ex-dividend days (FIX 3) ---------------------------
            # GUARD 2 excludes exactly the days the dividend normalisation
            # affects, so on its own it can never see a dividend-basis error.
            # On an ex-div date, Yahoo's own multiplier gives back the dividend
            # it used:  D = C_{t-1} * (1 - (A_{t-1}/A_t) * (C_t/C_{t-1}))
            # Compare that against the dividend this code used.
            ex = (div > 0) & ret.notna() & ret_adj.notna() & (adj > 0)
            if ex.any():
                implied = close.shift(1) * (
                    1 - (adj.shift(1) / adj) * (close / close.shift(1)))
                dgap = ((implied[ex] - div[ex]).abs() / close.shift(1)[ex] * 100)
                worst_dt, worst = dgap.idxmax(), dgap.max()
                print(f"  [{symbol}] dividend-basis check: max gap "
                      f"{worst:.2e}pp on {worst_dt.date()} "
                      f"({int(ex.sum())} ex-dividend days)")
                if worst > DIV_BASIS_TOLERANCE_PCT:
                    diagnostics.append(
                        f"{symbol}: dividend basis differs from Adj Close by "
                        f"{worst:.4f}pp on {worst_dt.date()} (may be Yahoo's "
                        f"own pre-split dividend handling)")

        returns_dict[symbol] = ret

    # FIX 4: an empty ticker used to be concatenated in as a RangeIndex series.
    # Mixing index types made the result's dtype depend on the data: with every
    # ticker empty it became an empty RangeIndex and the comparison below raised
    # TypeError (reproduced on pandas 2.3.3 and 3.0.x); with some tickers empty
    # it silently became an object Index. Keep empty tickers out of the concat
    # and add them back as NaN columns, so the index is always a DatetimeIndex.
    usable = {sym: s for sym, s in returns_dict.items() if len(s)}
    if not usable:
        print("\n  No usable data returned for any ticker.")
        diagnostics.append("no usable data returned for any ticker")
        return pd.DataFrame(columns=TICKERS), diagnostics, (start, end)

    df = pd.concat(list(usable.values()), axis=1)
    for sym in TICKERS:
        if sym not in df.columns:
            df[sym] = float("nan")
    # The renderer addresses cells positionally as values[r, c] against TICKERS,
    # so the column order has to be pinned rather than left to dict ordering.
    df = df[TICKERS]
    df.index = pd.DatetimeIndex(df.index)
    df.index.name = "Date"

    five_yr_start = pd.Timestamp(end - timedelta(days=LOOKBACK_YEARS * 365))
    df = df[df.index >= five_yr_start].dropna(how="all")
    df.index = pd.to_datetime(df.index).strftime("%Y-%m-%d")

    if diagnostics:
        print("\n  !! VALIDATION WARNINGS")
        for d in diagnostics:
            print(f"     - {d}")
    else:
        print(f"\n  Validation passed: no move beyond "
              f"+/-{MAX_PLAUSIBLE_MOVE_PCT:g}%, zero-dividend days reconcile "
              f"to Adj Close, ex-dividend days reconcile on dividend basis.")

    return df.sort_index(ascending=False), diagnostics, (start, end)


# -----------------------------------------------------------------------------
# STEP 5: Fast Canvas Table widget
# -----------------------------------------------------------------------------
class CanvasTable(tk.Frame):
    """DataFrame as a colour-coded table drawn with raw Canvas primitives."""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self._df: pd.DataFrame | None = None

        # FIX 7: the header used to be packed in the outer frame while the body
        # sat next to a vertical scrollbar, making the header 13px wider. The
        # two xview fractions then mapped to different pixel offsets and the
        # header drifted from the columns once scrolled. Putting both in the
        # same grid column makes them share a width by construction.
        grid = tk.Frame(self)
        grid.pack(fill="both", expand=True)

        self._hdr_canvas = tk.Canvas(grid, height=HDR_H, bg=COL_HDR_BG,
                                     highlightthickness=0)
        self._body = tk.Canvas(grid, bg="white", highlightthickness=0)
        self._vsb = tk.Scrollbar(grid, orient="vertical")
        self._hsb = tk.Scrollbar(grid, orient="horizontal")

        self._hdr_canvas.grid(row=0, column=0, sticky="ew")
        self._body.grid(row=1, column=0, sticky="nsew")
        self._vsb.grid(row=1, column=1, sticky="ns")
        self._hsb.grid(row=2, column=0, sticky="ew")
        grid.grid_rowconfigure(1, weight=1)
        grid.grid_columnconfigure(0, weight=1)

        self._body.configure(yscrollcommand=self._vsb.set,
                             xscrollcommand=self._hsb.set)
        self._vsb.config(command=self._body.yview)
        self._hsb.config(command=self._on_hscroll)
        self._hdr_canvas.configure(scrollregion=(0, 0, TOTAL_W, HDR_H))

        for w in (self._body, self._hdr_canvas):
            w.bind("<MouseWheel>", self._on_mousewheel)
            w.bind("<Button-4>", lambda e: self._body.yview_scroll(-1, "units"))
            w.bind("<Button-5>", lambda e: self._body.yview_scroll(1, "units"))

        self._xs = []
        x = 0
        for w in COL_WIDTHS:
            self._xs.append(x)
            x += w

    def _on_hscroll(self, *args):
        self._body.xview(*args)
        self._hdr_canvas.xview(*args)

    def _on_mousewheel(self, event):
        # Windows sends multiples of 120; macOS sends small values where
        # integer division would round to zero and the wheel would die.
        step = int(-event.delta / 120)
        if step == 0:
            step = -1 if event.delta > 0 else 1
        self._body.yview_scroll(step, "units")

    def load(self, df: pd.DataFrame):
        self._df = df
        self._draw()

    def _draw(self):
        if self._df is None:
            return
        df = self._df
        n_rows = len(df)

        self._body.delete("all")
        self._hdr_canvas.delete("all")
        self._body.configure(scrollregion=(0, 0, TOTAL_W, n_rows * ROW_H))
        self._hdr_canvas.configure(scrollregion=(0, 0, TOTAL_W, HDR_H))

        col_names = ["Date"] + [f"{sym} ({M7[sym]})" for sym in TICKERS]
        for c, (label, x) in enumerate(zip(col_names, self._xs)):
            x2 = x + COL_WIDTHS[c]
            self._hdr_canvas.create_rectangle(x, 0, x2, HDR_H,
                                              fill=COL_HDR_BG, outline=COL_GRID)
            self._hdr_canvas.create_text((x + x2) // 2, HDR_H // 2, text=label,
                                         fill=COL_HDR_FG, font=FONT_HDR,
                                         anchor="center")
        if n_rows == 0:
            self._body.create_text(TOTAL_W // 2, 40, text="No data",
                                   font=FONT_HDR, fill="#888")
            return

        rows   = df.index.tolist()
        values = df.values

        for r in range(n_rows):
            y1 = r * ROW_H
            y2 = y1 + ROW_H

            x1, x2 = self._xs[0], self._xs[0] + COL_WIDTHS[0]
            self._body.create_rectangle(x1, y1, x2, y2,
                                        fill=COL_DATE, outline=COL_GRID)
            self._body.create_text((x1 + x2) // 2, (y1 + y2) // 2,
                                   text=rows[r], font=FONT_DATE, anchor="center")

            for c in range(len(TICKERS)):
                val = values[r, c]
                x1  = self._xs[c + 1]
                x2  = x1 + COL_WIDTHS[c + 1]

                if val is None or (isinstance(val, float) and math.isnan(val)):
                    bg, txt = COL_FLAT, "-"
                elif val > 0:
                    bg, txt = COL_POS, f"+{val:.4f}%"
                elif val < 0:
                    bg, txt = COL_NEG, f"{val:.4f}%"
                else:
                    bg, txt = COL_FLAT, "0.0000%"

                self._body.create_rectangle(x1, y1, x2, y2, fill=bg,
                                            outline=COL_GRID)
                self._body.create_text(x2 - 6, (y1 + y2) // 2, text=txt,
                                       font=FONT_CELL, anchor="e")


# -----------------------------------------------------------------------------
# STEP 6: Main App
# -----------------------------------------------------------------------------
class M7ReturnApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Magnificent 7 - Daily Total Return")
        self.geometry("1150x700")
        self.minsize(900, 500)
        self.configure(bg="#f0f2f5")
        self._df = None
        self._window = current_window()
        self._build_ui()
        self.after(300, self._start_fetch)

    def _build_ui(self):
        header = tk.Frame(self, bg="#0d1b2a", pady=14)
        header.pack(fill="x")
        tk.Label(header, text="  Magnificent 7  |  Daily Total Return",
                 font=("Helvetica", 17, "bold"),
                 bg="#0d1b2a", fg="white").pack(side="left", padx=18)
        # A StringVar now, because the window moves with each refresh.
        self.subtitle_var = tk.StringVar()
        tk.Label(header, textvariable=self.subtitle_var, font=("Helvetica", 9),
                 bg="#0d1b2a", fg="#8ab4d4").pack(side="left", padx=6)
        self._set_subtitle(*self._window)

        toolbar = tk.Frame(self, bg="#e4e8ef", pady=7)
        toolbar.pack(fill="x")
        self.refresh_btn = ttk.Button(toolbar, text="Refresh",
                                      command=self._start_fetch)
        self.refresh_btn.pack(side="left", padx=10)
        self.export_btn = ttk.Button(toolbar, text="Export to CSV",
                                     command=self._export_csv)
        self.export_btn.pack(side="left", padx=4)
        self.export_btn.config(state="disabled")

        for colour, label in [(COL_POS, "Positive"), (COL_NEG, "Negative"),
                              (COL_FLAT, "Zero")]:
            box = tk.Frame(toolbar, bg=colour, width=14, height=14,
                           relief="solid", bd=1)
            box.pack(side="left", padx=(10, 2))
            box.pack_propagate(False)
            tk.Label(toolbar, text=label, font=("Helvetica", 8),
                     bg="#e4e8ef").pack(side="left", padx=(0, 4))

        self.summary_var = tk.StringVar(value="")
        tk.Label(toolbar, textvariable=self.summary_var, font=("Helvetica", 9),
                 bg="#e4e8ef", fg="#444").pack(side="right", padx=14)

        self.progress = ttk.Progressbar(self, mode="determinate", length=400)
        self.progress.pack(pady=(6, 0))

        self._table = CanvasTable(self, bg="#f0f2f5")
        self._table.pack(fill="both", expand=True, padx=10, pady=(4, 0))

        self.status_var = tk.StringVar(value="Starting up ...")
        self.status_lbl = tk.Label(self, textvariable=self.status_var, anchor="w",
                                   relief="sunken", bg="#dde2ea",
                                   font=("Helvetica", 9), pady=3)
        self.status_lbl.pack(fill="x", side="bottom")

    def _set_subtitle(self, start, end):
        five_yr = end - timedelta(days=LOOKBACK_YEARS * 365)
        self.subtitle_var.set(
            "(Close_t + Div_t - Close_{t-1}) / Close_{t-1}   "
            f"|   {five_yr}  ->  {end}")

    # ---------------------------------------------------------------- fetch
    def _start_fetch(self):
        if self.refresh_btn["state"] == "disabled":
            return                      # a fetch is already in flight
        self.refresh_btn.config(state="disabled")
        self.export_btn.config(state="disabled")
        self.status_var.set("Fetching prices & dividends ...")
        self.status_lbl.config(bg="#dde2ea", fg="black")
        self.progress.config(mode="indeterminate")
        self.progress.start(12)
        self._t0 = time.perf_counter()
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self):
        try:
            df, diagnostics, window = compute_daily_returns()
            self.after(0, self._on_data_ready, df, diagnostics, window)
        except Exception as exc:
            self.after(0, self._show_error, str(exc))

    def _reset_progress(self):
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)

    def _on_data_ready(self, df, diagnostics, window):
        elapsed = time.perf_counter() - getattr(self, "_t0", time.perf_counter())
        self._df = df
        self._window = window
        self._set_subtitle(*window)
        self._table.load(df)
        self._reset_progress()
        self.refresh_btn.config(state="normal")
        self.export_btn.config(state="normal" if len(df) else "disabled")

        n = len(df)
        self.summary_var.set(f"{n} trading days")
        stamp = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        print(f"\n  Refresh completed in {elapsed:.2f}s")

        if diagnostics:
            self.status_lbl.config(bg="#ffe0e0", fg="#8b0000")
            self.status_var.set(
                f"{n} trading days | {elapsed:.1f}s | Refreshed: {stamp} | "
                f"{len(diagnostics)} VALIDATION WARNING(S): {diagnostics[0]}"
                + (" ... see console" if len(diagnostics) > 1 else ""))
        else:
            self.status_lbl.config(bg="#dde2ea", fg="black")
            self.status_var.set(
                f"{n} trading days | {elapsed:.1f}s | Refreshed: {stamp} | "
                f"Validation passed")

    def _export_csv(self):
        if self._df is None or len(self._df) == 0:
            messagebox.showwarning("No Data", "Please wait for data to load first.")
            return
        path = asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"m7_daily_returns_{self._window[1]}.csv",
        )
        if path:
            self._df.to_csv(path)
            messagebox.showinfo("Exported", f"Saved to:\n\n{path}")

    def _show_error(self, message: str):
        # FIX 5: previously this left Export disabled and the progress bar in
        # indeterminate mode, so a failed refresh locked the user out of data
        # that was already loaded and parked a stray block on the bar.
        self._reset_progress()
        self.refresh_btn.config(state="normal")
        self.export_btn.config(
            state="normal" if self._df is not None and len(self._df) else "disabled")
        self.status_lbl.config(bg="#ffe0e0", fg="#8b0000")
        self.status_var.set(f"Error: {message}")
        messagebox.showerror("Fetch Error",
                             f"Something went wrong:\n\n{message}\n\n"
                             "Please check your internet and try Refresh.")


# -----------------------------------------------------------------------------
# STEP 7: Launch
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app = M7ReturnApp()
    app.mainloop()
