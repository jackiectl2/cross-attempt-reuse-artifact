"""Shared matplotlib style for the paper's data figures (ACM sigconf, A4, two columns).
Every figure script reads only files under <project>/results/ and writes a vector PDF next to itself."""
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RES = os.path.join(ROOT, "results")
OUT = os.path.dirname(os.path.abspath(__file__))
FULL_W, COL_W = 7.0, 3.33          # inches: text width and column width of the A4 sigconf layout

matplotlib.rcParams.update({
    "font.size": 7.5, "font.family": "serif", "font.serif": ["STIXGeneral", "DejaVu Serif"],
    "mathtext.fontset": "stix", "axes.labelsize": 7.5, "axes.titlesize": 7.5, "xtick.labelsize": 7,
    "ytick.labelsize": 7, "legend.fontsize": 6.8, "axes.linewidth": 0.6, "xtick.major.width": 0.6,
    "ytick.major.width": 0.6, "lines.linewidth": 1.1, "lines.markersize": 3.5, "axes.spines.top": False,
    "axes.spines.right": False, "legend.frameon": False, "pdf.fonttype": 42, "savefig.bbox": "tight",
    "savefig.pad_inches": 0.02, "hatch.linewidth": 0.6,
})
# Okabe-Ito (colorblind-safe)
C = {"blue": "#0072B2", "orange": "#E69F00", "green": "#009E73", "vermilion": "#D55E00", "sky": "#56B4E9",
     "purple": "#CC79A7", "gray": "#8C8C8C", "black": "#000000"}


def load(*path):
    with open(os.path.join(RES, *path)) as f:
        return json.load(f)


def save(fig, name):
    path = os.path.join(OUT, name + ".pdf")
    fig.savefig(path)
    print("saved", path)
