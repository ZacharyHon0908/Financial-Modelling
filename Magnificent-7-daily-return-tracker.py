
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

END_DATE   = date.today() - timedelta(days=1)
START_DATE = END_DATE - timedelta(days=5 * 365 + 5)

# --- validation thresholds ---------------------------------------------------
# No mega-cap has ever moved this much in one session; anything above it is a
# data or corporate-action problem, not a market move.
MAX_PLAUSIBLE_MOVE_PCT = 40.0
# On a zero-dividend day the two return definitions are algebraically identical,
# so any gap beyond numerical noise means the split basis is inconsistent.
#
# Sizing this threshold: a genuine split-basis error is off by the split RATIO,
# which shows up as hundreds of percentage points (a 10:1 split produces a
# ~909pp gap). The noise floor, driven by float precision and the finite
# precision of Yahoo's own Adj Close, sits around 1e-4 pp. 0.01pp (one basis
# point) sits three orders of magnitude above the noise and four below the
# smallest real signal, so it cannot fire on rounding and cannot miss a split.
ADJ_CLOSE_TOLERANCE_PCT = 0.01

# Visual constants
ROW_H      = 22
HDR_H      = 28
COL_WIDTHS = [108] + [132] * len(TICKERS)   # Date + 7 stock columns
TOTAL_W    = sum(COL_WIDTHS)

COL_POS    = "#c6efce"   # green
COL_NEG    = "#ffc7ce"   # red
COL_FLAT   = "#ffffff"   # white / zero
COL_DATE   = "#dde3f0"   # blue-grey date column
COL_HDR_BG = "#0d1b2a"   # header background
COL_HDR_FG = "white"
COL_GRID   = "#cccccc"   # grid lines

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
    Put RAW dividends onto the same split basis as Yahoo's already-adjusted Close.

    A dividend paid before an N-for-1 split is quoted in old-share terms, so it
    is N times too large relative to the back-adjusted price on that date.
    Divide it by the cumulative ratio of every split that happened AFTER it.

    NOTE: this is deliberately NOT applied to Close. Yahoo already split-adjusts
    OHLC; doing it twice is the bug this file fixes.
    """
    d = dividends.copy()
    for split_dt, ratio in splits.sort_index().items():
        if ratio is None or ratio <= 0:
            continue
        d.loc[d.index < split_dt] /= ratio
    return d


def fetch_symbol(symbol: str):
    """Return (close, dividends_on_split_basis, adj_close, splits_applied)."""
    ticker_obj = yf.Ticker(symbol)
    hist = ticker_obj.history(
        start=START_DATE.isoformat(),
        end=(END_DATE + timedelta(days=1)).isoformat(),
        auto_adjust=False,      # keep raw Close AND Adj Close so we can cross-check
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

    # Every split from the start of the window onwards affects earlier dividends.
    splits = ticker_obj.splits
    splits_applied = pd.Series(dtype=float)
    if splits is not None and not splits.empty:
        splits.index = to_naive(splits.index)
        splits_applied = splits[splits.index >= pd.Timestamp(START_DATE)]
        for split_dt, ratio in splits_applied.sort_index().items():
            print(f"  [{symbol}] split {ratio:g}:1 on {split_dt.date()} "
                  f"-> dividends before this date divided by {ratio:g}")

    if not splits_applied.empty:
        div = normalise_dividends_to_split_basis(div, splits_applied)

    # No rounding here. Rounding prices before the Adj Close reconciliation
    # raises the numerical noise floor and makes the guard fire on nothing.
    # Rounding belongs in the display layer only.
    for s in (close, div, adj):
        s.name = symbol
    return close, div, adj, splits_applied


def compute_daily_returns():
    """Return (dataframe_of_returns_pct, list_of_diagnostic_strings)."""
    returns_dict: dict[str, pd.Series] = {}
    diagnostics: list[str] = []

    for symbol in TICKERS:
        print(f"  Processing {symbol} ({M7[symbol]}) ...")
        close, div, adj, _ = fetch_symbol(symbol)

        if close.empty:
            returns_dict[symbol] = pd.Series(dtype=float, name=symbol)
            diagnostics.append(f"{symbol}: no data returned")
            continue

        # Explicit daily total return, in percent.
        ret = (close + div - close.shift(1)) / close.shift(1) * 100
        ret.name = symbol

        # --- GUARD 1: plausibility -------------------------------------------
        extreme = ret[ret.abs() > MAX_PLAUSIBLE_MOVE_PCT].dropna()
        for dt, val in extreme.items():
            diagnostics.append(
                f"{symbol}: implausible {val:+.2f}% on {dt.date()} "
                f"(check corporate actions)"
            )

        # --- GUARD 2: cross-check against Yahoo's Adj Close -------------------
        # On a zero-dividend day Adj Close differs from Close by the same
        # constant on both sides of the ratio, so the two returns must agree.
        if not adj.empty:
            ret_adj = (adj / adj.shift(1) - 1) * 100
            no_div = (div == 0) & ret.notna() & ret_adj.notna()
            gap = (ret[no_div] - ret_adj[no_div]).abs()
            if not gap.empty:
                worst_dt, worst = gap.idxmax(), gap.max()
                # Always report the observed floor, pass or fail. Seeing the
                # residual is what tells you the guard is alive and where the
                # numerical noise actually sits.
                print(f"  [{symbol}] Adj Close reconciliation: max gap "
                      f"{worst:.2e}pp on {worst_dt.date()} "
                      f"({len(gap)} zero-dividend days checked)")
                if worst > ADJ_CLOSE_TOLERANCE_PCT:
                    diagnostics.append(
                        f"{symbol}: split basis mismatch, {worst:.4f}pp gap vs "
                        f"Adj Close on {worst_dt.date()}"
                    )

        returns_dict[symbol] = ret

    df = pd.concat(list(returns_dict.values()), axis=1)
    df.index.name = "Date"
    five_yr_start = pd.Timestamp(date.today() - timedelta(days=1)
                                 - timedelta(days=5 * 365))
    df = df[df.index >= five_yr_start].dropna(how="all")
    df.index = pd.to_datetime(df.index).strftime("%Y-%m-%d")

    if diagnostics:
        print("\n  !! VALIDATION WARNINGS")
        for d in diagnostics:
            print(f"     - {d}")
    else:
        print(f"\n  Validation passed: no move beyond "
              f"+/-{MAX_PLAUSIBLE_MOVE_PCT:g}%, all zero-dividend days "
              f"reconcile to Adj Close.")

    return df.sort_index(ascending=False), diagnostics


# -----------------------------------------------------------------------------
# STEP 5: Fast Canvas Table widget
# -----------------------------------------------------------------------------
class CanvasTable(tk.Frame):
    """
    Renders a DataFrame as a colour-coded table using raw Canvas primitives.
    Only draws rectangles + text - no per-cell widget objects.
    """

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self._df: pd.DataFrame | None = None

        self._hdr_canvas = tk.Canvas(self, height=HDR_H, bg=COL_HDR_BG,
                                     highlightthickness=0)
        self._hdr_canvas.pack(fill="x", side="top")

        body_frame = tk.Frame(self)
        body_frame.pack(fill="both", expand=True)

        self._vsb = tk.Scrollbar(body_frame, orient="vertical")
        self._hsb = tk.Scrollbar(body_frame, orient="horizontal")
        self._vsb.grid(row=0, column=1, sticky="ns")
        self._hsb.grid(row=1, column=0, sticky="ew")
        body_frame.grid_rowconfigure(0, weight=1)
        body_frame.grid_columnconfigure(0, weight=1)

        self._body = tk.Canvas(body_frame, bg="white",
                               yscrollcommand=self._vsb.set,
                               xscrollcommand=self._hsb.set,
                               highlightthickness=0)
        self._body.grid(row=0, column=0, sticky="nsew")
        self._vsb.config(command=self._body.yview)
        self._hsb.config(command=self._on_hscroll)

        # Only the BODY drives the horizontal scrollbar. Letting the header
        # drive it too made the two canvases fight over the same slider.
        self._hdr_canvas.configure(scrollregion=(0, 0, TOTAL_W, HDR_H))

        self._body.bind("<MouseWheel>", self._on_mousewheel)
        self._body.bind("<Button-4>", lambda e: self._body.yview_scroll(-1, "units"))
        self._body.bind("<Button-5>", lambda e: self._body.yview_scroll(1, "units"))

        self._xs = []
        x = 0
        for w in COL_WIDTHS:
            self._xs.append(x)
            x += w

    def _on_hscroll(self, *args):
        self._body.xview(*args)
        self._hdr_canvas.xview(*args)

    def _on_mousewheel(self, event):
        # Windows sends multiples of 120; macOS sends small values, where
        # integer division would round down to zero and the wheel would die.
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
        self._build_ui()
        self.after(300, self._start_fetch)

    def _build_ui(self):
        header = tk.Frame(self, bg="#0d1b2a", pady=14)
        header.pack(fill="x")
        tk.Label(header, text="  Magnificent 7  |  Daily Total Return",
                 font=("Helvetica", 17, "bold"),
                 bg="#0d1b2a", fg="white").pack(side="left", padx=18)
        five_yr = date.today() - timedelta(days=1) - timedelta(days=5 * 365)
        tk.Label(header,
                 text=f"(Close_t + Div_t - Close_{{t-1}}) / Close_{{t-1}}   "
                      f"|   {five_yr}  ->  {END_DATE}",
                 font=("Helvetica", 9),
                 bg="#0d1b2a", fg="#8ab4d4").pack(side="left", padx=6)

        toolbar = tk.Frame(self, bg="#e4e8ef", pady=7)
        toolbar.pack(fill="x")
        self.refresh_btn = ttk.Button(toolbar, text="Refresh",
                                      command=self._start_fetch)
        self.refresh_btn.pack(side="left", padx=10)
        self.export_btn = ttk.Button(toolbar, text="Export to CSV",
                                     command=self._export_csv)
        self.export_btn.pack(side="left", padx=4)

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

        self.progress = ttk.Progressbar(self, mode="indeterminate", length=400)
        self.progress.pack(pady=(6, 0))

        self._table = CanvasTable(self, bg="#f0f2f5")
        self._table.pack(fill="both", expand=True, padx=10, pady=(4, 0))

        self.status_var = tk.StringVar(value="Starting up ...")
        self.status_lbl = tk.Label(self, textvariable=self.status_var, anchor="w",
                                   relief="sunken", bg="#dde2ea",
                                   font=("Helvetica", 9), pady=3)
        self.status_lbl.pack(fill="x", side="bottom")

    def _start_fetch(self):
        self.refresh_btn.config(state="disabled")
        self.export_btn.config(state="disabled")
        self.status_var.set("Fetching prices & dividends ... (~20 seconds)")
        self.status_lbl.config(bg="#dde2ea", fg="black")
        self.progress.config(mode="indeterminate")
        self.progress.start(12)
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self):
        try:
            df, diagnostics = compute_daily_returns()
            self.after(0, self._on_data_ready, df, diagnostics)
        except Exception as exc:
            self.after(0, self._show_error, str(exc))

    def _on_data_ready(self, df: pd.DataFrame, diagnostics: list[str]):
        self._df = df
        self._table.load(df)
        self.progress.stop()
        # Leaving it in indeterminate mode parks a stray block on the bar.
        self.progress.config(mode="determinate", value=0)
        self.refresh_btn.config(state="normal")
        self.export_btn.config(state="normal")

        n = len(df)
        self.summary_var.set(f"{n} trading days")
        stamp = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")

        if diagnostics:
            self.status_lbl.config(bg="#ffe0e0", fg="#8b0000")
            self.status_var.set(
                f"{n} trading days | Refreshed: {stamp} | "
                f"{len(diagnostics)} VALIDATION WARNING(S): {diagnostics[0]}"
                + (" ... see console" if len(diagnostics) > 1 else "")
            )
        else:
            self.status_lbl.config(bg="#dde2ea", fg="black")
            self.status_var.set(
                f"{n} trading days | Refreshed: {stamp} | Validation passed"
            )

    def _export_csv(self):
        if self._df is None:
            messagebox.showwarning("No Data", "Please wait for data to load first.")
            return
        path = asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"m7_daily_returns_{END_DATE}.csv",
        )
        if path:
            self._df.to_csv(path)
            messagebox.showinfo("Exported", f"Saved to:\n\n{path}")

    def _show_error(self, message: str):
        self.progress.stop()
        self.refresh_btn.config(state="normal")
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
