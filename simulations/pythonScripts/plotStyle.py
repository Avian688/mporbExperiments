"""OrbTCP figure layouts, with portable scientific styling for aggregate plots."""
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
import shutil

CDF_SIZE = (5, 2)
RTT_SIZE = (4.5, 1.2)
TRACE_SIZE = (17, 5)
MARKERS = {'Delta': '^', 'Omega': '.', 'Alpha': '_', 'OrbUncoupled': 'o', 'CubicUncoupled': 'x'}


@lru_cache(maxsize=1)
def latex_available():
    if not all(shutil.which(tool) for tool in ('latex', 'dvipng')):
        return False
    from matplotlib.texmanager import TexManager
    try:
        TexManager.make_dvi('lp', 10)
    except (OSError, RuntimeError):
        return False
    return True


@contextmanager
def plot_style(aggregate=False, cdf=False):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    # OrbTCP uses the SciencePlots base style for aggregate figures. These
    # settings keep that appearance without requiring an extra Python package.
    # Reference: github.com/garrettj403/SciencePlots
    # (src/scienceplots/styles/science.mplstyle).
    params = {'text.usetex': False}
    if aggregate:
        params.update({
            'font.family': 'serif', 'mathtext.fontset': 'dejavuserif',
            'axes.linewidth': .5, 'grid.linewidth': .5, 'lines.linewidth': 1.,
            'legend.frameon': False, 'axes.grid': False,
            'axes.labelsize': 'large' if cdf else 'medium',
            'xtick.labelsize': 'large' if cdf else 'medium',
            'ytick.labelsize': 'large' if cdf else 'medium',
            'axes.titlesize': 'medium',
            'text.latex.preamble': r'\usepackage{amsmath} \usepackage{amssymb}',
        })
        for axis in ('xtick', 'ytick'):
            params.update({f'{axis}.direction': 'in', f'{axis}.major.size': 3,
                           f'{axis}.major.width': .5, f'{axis}.minor.size': 1.5,
                           f'{axis}.minor.width': .5, f'{axis}.minor.visible': True})
        params.update({'xtick.top': True, 'ytick.right': True})
    with plt.style.context('default'), plt.rc_context(params):
        if aggregate:
            plt.rcParams['text.usetex'] = latex_available()
        yield


def save_figure(fig, output, aggregate=False):
    output = Path(output)
    for suffix in ('png', 'pdf'):
        fig.savefig(output.with_suffix('.' + suffix), dpi=300 if suffix == 'png' else 1080,
                    bbox_inches='tight' if aggregate else None, pad_inches=.02)
