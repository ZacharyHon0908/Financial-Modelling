"""
================================================
  Magnificent 7 – Daily Total Return Tracker
================================================
• Formula: (Close_t + Div_t − Close_{t-1}) / Close_{t-1}
• Per-cell colour:  green=positive  red=negative  white=zero
• High-performance: Canvas primitives only (no per-cell widgets)
• Auto-installs packages on first run
"""

import subprocess, sys

# ─────────────────────────────────────────────────────────────────────────────
# STEP 0: Auto-install
# ─────────────────────────────────────────────────────────────────────────────
def _install_if_missing(packages: list[str]):
    for pkg in packages:
        try:
            __import__(pkg)
        except ImportError:
            print(f"[Setup] '{pkg}' not found → installing …")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pkg],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            print(f"[Setup] '{pkg}' installed ✓")

_install_if_missing(["yfinance", "pandas"])

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: Imports
# ─────────────────────────────────────────────────────────────────────────────
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.filedialog import asksaveasfilename
import threading
import pandas as pd
import yfinance as yf
from datetime import date, timedelta, datetime

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: Config
# ─────────────────────────────────────────────────────────────────────────────
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
EVEN_TINT  = "#f7f8fc"   # alternating row tint applied on top of cell colour
ODD_TINT   = "#eef0f8"

FONT_HDR  = ("Helvetica",   10, "bold")
FONT_CELL = ("Courier New", 10)
FONT_DATE = ("Helvetica",   10)

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: Timezone helper
# ─────────────────────────────────────────────────────────────────────────────
def to_naive(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    return idx.normalize()

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4: Data fetch & return computation
# ─────────────────────────────────────────────────────────────────────────────
def _apply_split_adj(series: pd.Series, splits_in_range: pd.Series) -> pd.Series:
    s = series.copy()
    for split_dt, ratio in splits_in_range.sort_index().items():
        if ratio <= 0:
            continue
        s.loc[s.index < split_dt] /= ratio
    return s


def fetch_price_and_dividends(symbol: str):
    ticker_obj = yf.Ticker(symbol)
    hist = ticker_obj.history(
        start=START_DATE.isoformat(),
        end=(END_DATE + timedelta(days=1)).isoformat(),
        auto_adjust=False,
        actions=True,
    )
    if hist.empty:
        empty = pd.Series(dtype=float, name=symbol)
        return empty, empty

    hist.index = to_naive(hist.index)
    close = hist["Close"].copy().astype(float)
    div   = hist["Dividends"].copy().astype(float)

    splits = ticker_obj.splits
    splits_in_range = pd.Series(dtype=float)
    if not splits.empty:
        splits.index = to_naive(splits.index)
        ts_start = pd.Timestamp(START_DATE)
        ts_end   = pd.Timestamp(END_DATE)
        splits_in_range = splits[(splits.index >= ts_start) & (splits.index <= ts_end)]
        for split_dt, ratio in splits_in_range.sort_index().items():
            print(f"  [{symbol}] Split {ratio}:1 on {split_dt.date()} applied ✓")

    if not splits_in_range.empty:
        close = _apply_split_adj(close, splits_in_range)
        div   = _apply_split_adj(div,   splits_in_range)

    close.name = symbol
    div.name   = symbol
    return close.round(6), div.round(6)


def compute_daily_returns() -> pd.DataFrame:
    returns_dict = {}
    for symbol in TICKERS:
        print(f"  Processing {symbol} ({M7[symbol]}) …")
        close, div = fetch_price_and_dividends(symbol)
        if close.empty:
            returns_dict[symbol] = pd.Series(dtype=float, name=symbol)
            continue
        ret = (close + div - close.shift(1)) / close.shift(1) * 100
        ret.name = symbol
        returns_dict[symbol] = ret

    df = pd.concat(list(returns_dict.values()), axis=1)
    df.index.name = "Date"
    five_yr_start = pd.Timestamp(date.today() - timedelta(days=1) - timedelta(days=5 * 365))
    df = df[df.index >= five_yr_start].dropna(how="all")
    df.index = pd.to_datetime(df.index).strftime("%Y-%m-%d")
    return df.sort_index(ascending=False)


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5: Fast Canvas Table widget
# ─────────────────────────────────────────────────────────────────────────────
class CanvasTable(tk.Frame):
    """
    Renders a DataFrame as a colour-coded table using raw Canvas primitives.
    Only draws rectangles + text — no per-cell widget objects.
    Supports vertical & horizontal scrolling.
    """

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self._df: pd.DataFrame | None = None

        # Header canvas (fixed, never scrolls vertically)
        self._hdr_canvas = tk.Canvas(self, height=HDR_H, bg=COL_HDR_BG,
                                     highlightthickness=0)
        self._hdr_canvas.pack(fill="x", side="top")

        # Body canvas + scrollbars
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
        self._vsb.config(command=self._on_vscroll)
        self._hsb.config(command=self._on_hscroll)

        # Sync header horizontal scroll with body
        self._body.bind("<Configure>", self._on_body_configure)
        self._body.bind("<MouseWheel>", self._on_mousewheel)
        self._body.bind("<Button-4>",   lambda e: self._body.yview_scroll(-1, "units"))
        self._body.bind("<Button-5>",   lambda e: self._body.yview_scroll( 1, "units"))

        # Precompute cumulative x positions
        self._xs = []
        x = 0
        for w in COL_WIDTHS:
            self._xs.append(x)
            x += w

    # ── Scrolling ─────────────────────────────────────────────────────────────
    def _on_vscroll(self, *args):
        self._body.yview(*args)

    def _on_hscroll(self, *args):
        self._body.xview(*args)
        self._hdr_canvas.xview(*args)

    def _on_mousewheel(self, event):
        self._body.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_body_configure(self, event):
        # Keep header canvas scroll region in sync with body
        self._hdr_canvas.configure(scrollregion=(0, 0, TOTAL_W, HDR_H),
                                   xscrollcommand=self._hsb.set)

    # ── Draw ──────────────────────────────────────────────────────────────────
    def load(self, df: pd.DataFrame):
        self._df = df
        self._draw()

    def _draw(self):
        if self._df is None:
            return
        df = self._df
        n_rows = len(df)
        total_height = n_rows * ROW_H

        # Clear both canvases
        self._body.delete("all")
        self._hdr_canvas.delete("all")

        # Set scroll regions
        self._body.configure(scrollregion=(0, 0, TOTAL_W, total_height))
        self._hdr_canvas.configure(scrollregion=(0, 0, TOTAL_W, HDR_H))

        # ── Draw header ───────────────────────────────────────────────────────
        col_names = ["Date"] + [
            f"{sym} ({M7[sym][:5]+'...' if len(M7[sym])>5 else M7[sym]})"
            for sym in TICKERS
        ]
        for c, (label, x) in enumerate(zip(col_names, self._xs)):
            x2 = x + COL_WIDTHS[c]
            self._hdr_canvas.create_rectangle(x, 0, x2, HDR_H,
                                              fill=COL_HDR_BG, outline=COL_GRID)
            self._hdr_canvas.create_text(
                (x + x2) // 2, HDR_H // 2,
                text=label, fill=COL_HDR_FG, font=FONT_HDR, anchor="center"
            )

        # ── Draw data rows (all at once — canvas handles virtualisation) ──────
        rows   = df.index.tolist()
        values = df.values   # numpy array for speed

        for r in range(n_rows):
            y1 = r * ROW_H
            y2 = y1 + ROW_H

            # Date cell
            x1, x2 = self._xs[0], self._xs[0] + COL_WIDTHS[0]
            self._body.create_rectangle(x1, y1, x2, y2,
                                        fill=COL_DATE, outline=COL_GRID)
            self._body.create_text(
                (x1 + x2) // 2, (y1 + y2) // 2,
                text=rows[r], font=FONT_DATE, anchor="center"
            )

            # Stock cells
            for c, sym in enumerate(TICKERS):
                val = values[r, c]
                x1  = self._xs[c + 1]
                x2  = x1 + COL_WIDTHS[c + 1]

                import math
                if val is None or (isinstance(val, float) and math.isnan(val)):
                    bg  = COL_FLAT
                    txt = "—"
                elif val > 0:
                    bg  = COL_POS
                    txt = f"+{val:.4f}%"
                elif val < 0:
                    bg  = COL_NEG
                    txt = f"{val:.4f}%"
                else:
                    bg  = COL_FLAT
                    txt = f"0.0000%"

                self._body.create_rectangle(x1, y1, x2, y2,
                                            fill=bg, outline=COL_GRID)
                self._body.create_text(
                    x2 - 6, (y1 + y2) // 2,
                    text=txt, font=FONT_CELL, anchor="e"
                )


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6: Main App
# ─────────────────────────────────────────────────────────────────────────────
class M7ReturnApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Magnificent 7 – Daily Total Return")
        self.geometry("1150x700")
        self.minsize(900, 500)
        self.configure(bg="#f0f2f5")
        self._df = None
        self._build_ui()
        self.after(300, self._start_fetch)

    def _build_ui(self):
        # Header bar
        header = tk.Frame(self, bg="#0d1b2a", pady=14)
        header.pack(fill="x")
        tk.Label(header, text="  ✦  Magnificent 7  |  Daily Total Return",
                 font=("Helvetica", 17, "bold"),
                 bg="#0d1b2a", fg="white").pack(side="left", padx=18)
        five_yr = date.today() - timedelta(days=1) - timedelta(days=5 * 365)
        tk.Label(header,
                 text=f"(Close_t + Div_t − Close_{{t-1}}) / Close_{{t-1}}   "
                      f"|   {five_yr}  →  {END_DATE}",
                 font=("Helvetica", 9),
                 bg="#0d1b2a", fg="#8ab4d4").pack(side="left", padx=6)

        # Toolbar
        toolbar = tk.Frame(self, bg="#e4e8ef", pady=7)
        toolbar.pack(fill="x")
        self.refresh_btn = ttk.Button(toolbar, text="🔄  Refresh", command=self._start_fetch)
        self.refresh_btn.pack(side="left", padx=10)
        self.export_btn = ttk.Button(toolbar, text="💾  Export to CSV", command=self._export_csv)
        self.export_btn.pack(side="left", padx=4)

        # Legend
        for colour, label in [(COL_POS, "Positive"), (COL_NEG, "Negative"), (COL_FLAT, "Zero")]:
            box = tk.Frame(toolbar, bg=colour, width=14, height=14, relief="solid", bd=1)
            box.pack(side="left", padx=(10, 2))
            box.pack_propagate(False)
            tk.Label(toolbar, text=label, font=("Helvetica", 8),
                     bg="#e4e8ef").pack(side="left", padx=(0, 4))

        self.summary_var = tk.StringVar(value="")
        tk.Label(toolbar, textvariable=self.summary_var, font=("Helvetica", 9),
                 bg="#e4e8ef", fg="#444").pack(side="right", padx=14)

        # Progress bar
        self.progress = ttk.Progressbar(self, mode="indeterminate", length=400)
        self.progress.pack(pady=(6, 0))

        # Canvas table (takes remaining space)
        self._table = CanvasTable(self, bg="#f0f2f5")
        self._table.pack(fill="both", expand=True, padx=10, pady=(4, 0))

        # Status bar
        self.status_var = tk.StringVar(value="Starting up …")
        tk.Label(self, textvariable=self.status_var, anchor="w", relief="sunken",
                 bg="#dde2ea", font=("Helvetica", 9), pady=3).pack(fill="x", side="bottom")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    def _start_fetch(self):
        self.refresh_btn.config(state="disabled")
        self.export_btn.config(state="disabled")
        self.status_var.set("⏳  Fetching prices & dividends … (~20 seconds)")
        self.progress.start(12)
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self):
        try:
            df = compute_daily_returns()
            self.after(0, self._on_data_ready, df)
        except Exception as exc:
            self.after(0, self._show_error, str(exc))

    def _on_data_ready(self, df: pd.DataFrame):
        self._df = df
        self._table.load(df)
        self.progress.stop()
        self.progress.config(value=0)
        self.refresh_btn.config(state="normal")
        self.export_btn.config(state="normal")
        n = len(df)
        self.summary_var.set(f"{n} trading days")
        self.status_var.set(
            f"✅  {n} trading days  |  "
            f"Refreshed: {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}"
        )

    # ── Export ────────────────────────────────────────────────────────────────
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
            messagebox.showinfo("Exported ✓", f"Saved to:\n\n{path}")

    def _show_error(self, message: str):
        self.progress.stop()
        self.refresh_btn.config(state="normal")
        self.status_var.set(f"❌  Error: {message}")
        messagebox.showerror("Fetch Error",
                             f"Something went wrong:\n\n{message}\n\n"
                             "Please check your internet and try Refresh.")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 7: Launch
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = M7ReturnApp()
    app.mainloop()
