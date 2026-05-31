"""Asset Forge UI — interactive control layer for the Houdini pipeline.

Phase 1: the **Layout section**. Gives live framing feedback on the laid-out
placeholders (the ``Layout`` SOP through ``Scene_LAYOUT_Cam``), a one-click
layout-preview render, and a layout-level image-generation block (N sequential
re-rolls via ``nano_banana_client``, pick the keeper).

The launch button opens a **floating panel split into [ Scene Viewer | Controls ]**
so the live viewport sits beside the controls as one tool, without disturbing the
user's main desktops. The controls are a Python Panel interface (``asset_forge``);
the Scene Viewer is pinned to the layout cam with the ``Layout`` geo soloed.

Design notes:
- ``Layout_OpenGL.picture`` is ``\`@path_previewlayout\``` (PDG-only). For the
  instant UI render we temporarily set it to the literal path, render
  synchronously, then restore — keeping the batch pipeline attribute-driven.
- Image generation is network-bound, so it runs on a worker thread and marshals
  results back to the GUI thread via Qt signals.

Launch from the HDA "Launch UI" button (``import asset_forge_ui;
asset_forge_ui.launch(kwargs['node'])``) or the Python Shell
(``asset_forge_ui.launch(hou.node('/obj/asset_bottles'))``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
from typing import Optional

import hou
from PySide6 import QtCore, QtGui, QtWidgets

# Resolve our own directory rather than hardcoding it, so the tool relocates
# cleanly with the repo.
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PYPANEL_FILE = os.path.join(TOOLS_DIR, "asset_forge.pypanel")
LAYOUT_INTERFACE = "asset_forge_layout"
ASSETS_INTERFACE = "asset_forge_assets"

if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

# Path of the instance the most recent launch targeted — read by the panel
# factories when the Python Panel system instantiates a stage widget.
_ACTIVE_INSTANCE: Optional[str] = None


# --------------------------------------------------------------------------- #
# Logging — Generate / Render failures go to the Houdini console *and* a log
# file, so a failed style-pass isn't lost in the one-line status label.
# --------------------------------------------------------------------------- #
log = logging.getLogger("asset_forge")
if not log.handlers:
    log.setLevel(logging.INFO)
    _console = logging.StreamHandler()
    _console.setFormatter(
        logging.Formatter("[asset_forge] %(levelname)s: %(message)s"))
    log.addHandler(_console)
    log.propagate = False


def _attach_log_file(inst: "hou.Node") -> Optional[str]:
    """Attach a file handler writing to ``<collection>/vNNN/asset_forge.log`` once.

    Idempotent — re-opening the panel won't stack duplicate handlers. Returns the
    log path, or ``None`` if it couldn't be set up."""
    try:
        path = os.path.join(os.path.dirname(layout_dir(inst)), "asset_forge.log")
    except Exception:
        return None
    target = os.path.abspath(path)
    for h in log.handlers:
        if isinstance(h, logging.FileHandler) \
                and os.path.abspath(getattr(h, "baseFilename", "")) == target:
            return path
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fh = logging.FileHandler(target)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(fh)
    except Exception:
        log.exception("could not open log file at %s", target)
        return None
    return path


# --------------------------------------------------------------------------- #
# Theme — single source of styling truth.
# This is the landing zone for the incoming design's CSS: translate its colour
# and spacing tokens into THEME, and any structural rules into _STYLESHEET.
# Widget code stays style-free (it only sets objectName / property hooks), so
# re-skinning never touches the Houdini wiring below.
# --------------------------------------------------------------------------- #
THEME = {
    "bg":         "#1a1a1a",
    "bg_dark":    "#262626",   # Results Viewer canvas
    "panel":      "#242424",
    "panel_2":    "#2d2d2d",   # card / prompt-bar surface (dark, matches Houdini)
    "line":       "#454545",   # hairline on dark surfaces
    "border":     "#333333",
    "text":       "#dddddd",
    "text_muted": "#888888",
    "muted_2":    "#b0b0b0",
    "accent":     "#4a9eff",
    "ok":         "#1f8a5b",   # done
    "warn":       "#d08a1f",   # generating
}

_STYLESHEET = """
QLabel#previewLabel {{
    background: {bg};
    border: 1px solid {border};
}}
QLabel#thumb {{
    border: 1px solid {border};
}}
QLabel#status {{
    color: {text_muted};
}}
QToolButton#sectionHeader {{
    border: none;
    text-align: left;
    padding: 6px 0 2px 0;
    font-weight: bold;
}}
QFrame#sectionRule {{
    color: {border};
}}

/* Context / density segmented control */
QPushButton#segItem {{
    border: 1px solid {border};
    background: {panel};
    color: {text};
    padding: 3px 12px;
}}
QPushButton#segItem:checked {{
    background: {accent};
    color: white;
    border-color: {accent};
}}

/* Asset card */
QFrame#assetCard {{
    border: 1px solid {line};
    border-left: 3px solid transparent;
    background: {panel_2};
}}
QFrame#assetCard[active="true"] {{
    border: 1px solid {accent};
    border-left: 3px solid {accent};
}}
QFrame#assetCard[included="false"] {{
    background: #232323;
}}
QLabel#cardName    {{ font-weight: 600; }}
QLabel#cardPath    {{ color: {text_muted}; }}
QLabel#cardMeta    {{ color: {text_muted}; }}
QLabel#assetThumb  {{ background: {bg}; border: 1px solid {border}; }}
QLabel#resultThumb {{ border: 1px solid {border}; }}
QLabel#resultThumb[keeper="true"] {{ border: 2px solid {accent}; }}

/* Results Viewer / Picker (dark) */
QDialog#picker, QWidget#pickerCanvas {{ background: {bg_dark}; color: {text}; }}
QLabel#pickerCell  {{ border: 1px solid #444; background: #2e2e2e; }}
QLabel#pickerCell[keeper="true"] {{ border: 2px solid {accent}; }}
QLabel#filmFrame   {{ border: 1px solid #444; }}
QLabel#filmFrame[active="true"] {{ border: 2px solid {accent}; }}
""".format(**THEME)


# --------------------------------------------------------------------------- #
# Instance / path helpers
# --------------------------------------------------------------------------- #
def find_instance(node: Optional[hou.Node] = None) -> hou.Node:
    """Return the asset_forge HDA instance to drive (walk up, or first in /obj)."""
    n = node
    while n is not None:
        if n.type().name().startswith("asset_forge"):
            return n
        n = n.parent()
    candidates = [
        c for c in hou.node("/obj").children()
        if c.type().name().startswith("asset_forge")
        and not c.name().startswith("DONT_USE")
    ]
    if not candidates:
        raise RuntimeError("No asset_forge instance found in /obj")
    return candidates[0]


def _layout_cam(inst: hou.Node) -> Optional[hou.Node]:
    """The ortho layout camera. Renamed ``Scene_Cam`` → ``Layout_Cam`` in
    ``asset_forge::1.1``; accept either so the panel works across HDA versions."""
    return inst.node("Layout_Cam") or inst.node("Scene_Cam")


def layout_dir(inst: hou.Node) -> str:
    coll = inst.parm("collection").eval()
    ver = int(inst.parm("version").eval())
    hip = hou.text.expandString("$HIP")
    return os.path.join(hip, str(coll), "v%03d" % ver, "layout")


def preview_path(inst: hou.Node) -> str:
    return os.path.join(layout_dir(inst), "layout_preview.jpg")


def generated_path(inst: hou.Node) -> str:
    return os.path.join(layout_dir(inst), "layout_generated.png")


def candidate_path(inst: hou.Node, index: int) -> str:
    return os.path.join(layout_dir(inst), "layout_gen_%02d.png" % index)


# --------------------------------------------------------------------------- #
# Per-asset data — assets.json (the asset list) + each asset's data.json
# (UI state, merged so we never clobber pipeline-written fields).
# Mirrors the layout-level path helpers above, but for the per-asset stage.
# --------------------------------------------------------------------------- #
def assets_json_path(inst: hou.Node) -> str:
    return os.path.join(layout_dir(inst), "assets.json")


def load_assets(inst: hou.Node) -> "list[dict]":
    """Read ``layout/assets.json`` → the asset list (``[]`` if missing/bad)."""
    path = assets_json_path(inst)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return data.get("assets", []) if isinstance(data, dict) else []


def asset_dir_of(asset: dict) -> str:
    """Absolute per-asset folder — expands ``$HIP`` etc. in ``asset_dir``."""
    return hou.text.expandString(asset.get("asset_dir", ""))


def asset_keeper_path(asset_dir: str) -> str:
    return os.path.join(asset_dir, "generated.png")


def asset_candidate_path(asset_dir: str, index: int) -> str:
    return os.path.join(asset_dir, "gen_%02d.png" % index)


def asset_input_path(asset_dir: str) -> str:
    """The isolated clay placeholder render — nano_banana's style-ref input.
    Written by :func:`render_asset_preview` from the chosen Asset_Cam."""
    return os.path.join(asset_dir, "preview.jpg")


# Per-asset camera presets → the Asset_Cam rig nodes (all hang off Asset_Null2).
# The card's CAMERA combo stores one of these labels in data.json; the live
# preview render points the Asset_OpenGL ROP at the matching camera.
ASSET_CAMS = {
    "Isometric": "Asset_Cam_Isometric",
    "Front":     "Asset_Cam_Front",
    "Side":      "Asset_Cam_Side",
    "Top":       "Asset_Cam_Top",
}
CAMERA_PRESETS = list(ASSET_CAMS)        # combo order; [0] is the default
DEFAULT_CAM = CAMERA_PRESETS[0]          # "Isometric" (the rig's standing cam)

# CONTEXT segmented switch → the input index of the Display node's `switch1`.
# Display soloing via display flags is impossible inside a locked HDA, so the
# HDA instead carries a `display_node` int parm wired to `Display/switch1`
# (input = ch("../../display_node")). The objmerges feed the switch in this
# order — Layout=0, Blockout/Centered=1, Generated=2 — so the UI just sets the
# parm and the switch picks the matching geo.
CONTEXT_INDEX = {
    "layout":    0,
    "blockout":  1,
    "generated": 2,
}


def asset_candidates(asset_dir: str) -> "list[str]":
    """Sorted ``gen_*.png`` candidate paths in a per-asset folder."""
    if not os.path.isdir(asset_dir):
        return []
    return [os.path.join(asset_dir, f)
            for f in sorted(os.listdir(asset_dir))
            if f.startswith("gen_") and f.lower().endswith(".png")]


def asset_thumb_path(asset_dir: str, mode: str) -> Optional[str]:
    """Best existing preview image for the given context, or ``None``.

    Generated → the keeper; Blockout → the isolated clay preview (the Asset_Cam
    render, refreshed by "Render Preview"); Layout → the styled render. Each
    falls back to whatever else is on disk so a card never goes blank.
    """
    names = {
        "generated": ["generated.png"],
        "blockout":  ["preview.jpg", "render.jpg"],
        "layout":    ["render.jpg", "preview.jpg"],
    }.get(mode, ["render.jpg", "preview.jpg"])
    for name in names:
        p = os.path.join(asset_dir, name)
        if os.path.isfile(p):
            return p
    return None


def load_asset_state(asset_dir: str) -> dict:
    """Read a per-asset ``data.json`` (``{}`` if absent/unreadable)."""
    path = os.path.join(asset_dir, "data.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_asset_state(asset_dir: str, **updates) -> None:
    """Merge ``updates`` into the asset's ``data.json`` and write it back.

    Never clobbers existing pipeline-written keys — only the keys we pass in.
    UI state lives under a ``ui`` sub-dict to stay clearly separated.
    """
    os.makedirs(asset_dir, exist_ok=True)
    data = load_asset_state(asset_dir)
    ui = dict(data.get("ui", {}))
    ui.update(updates)
    data["ui"] = ui
    path = os.path.join(asset_dir, "data.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# --------------------------------------------------------------------------- #
# Context display (drive the Display node's switch via the HDA `display_node`
# parm — works on a locked HDA, where display flags can't be set)
# --------------------------------------------------------------------------- #
def _set_display_context(inst: hou.Node, mode: str) -> None:
    """Show the geo for ``mode`` in the viewport by setting the instance's
    ``display_node`` parm, which drives ``Display/switch1``. No-op if the mode
    is unknown or the parm is missing (older HDA without the switch wiring)."""
    idx = CONTEXT_INDEX.get(mode)
    if idx is None:
        return
    p = inst.parm("display_node")
    if p is not None:
        p.set(idx)


def _asset_forge_scene_viewer() -> "Optional[hou.SceneViewer]":
    """The Scene Viewer tab of the Asset Forge floating panel, if open.

    Falls back to the desktop's current Scene Viewer when the panel has been
    re-docked, so the camera-follow still works after a layout rearrange."""
    for fp in hou.ui.floatingPanels():
        if fp.name() not in ("Asset Forge", "Asset_Forge"):
            continue
        for pane in fp.panes():
            for tab in pane.tabs():
                if tab.type() == hou.paneTabType.SceneViewer:
                    return tab
    return hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)


def _drive_viewport_camera(cam_node: "Optional[hou.Node]") -> None:
    """Point the Asset Forge Scene Viewer at ``cam_node`` (no-op if either the
    viewer or the camera is missing)."""
    sv = _asset_forge_scene_viewer()
    if sv is None or cam_node is None:
        return
    try:
        sv.curViewport().setCamera(cam_node)
    except Exception:
        pass


def _apply_viewport_look(sv: "Optional[hou.SceneViewer]") -> None:
    """Lock the Asset Forge Scene Viewer to the preferred look: both grids off
    (floor reference plane + ortho ruler) and Work Lights → Dome. The
    ``Headlight`` lighting mode means "use the work light"; ``workLightType``
    then selects which one (here a dome), rather than the scene's own lights."""
    if sv is None:
        return
    try:
        sv.referencePlane().setIsVisible(False)
        s = sv.curViewport().settings()
        s.setDisplayOrthoGrid(False)
        s.setLighting(hou.viewportLighting.Headlight)
        s.setWorkLightType(hou.viewportWorkLight.Domelight)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Per-asset preview render (Asset_Cam rig) + prompt expansion
# --------------------------------------------------------------------------- #
def _attr_map(inst: hou.Node, asset: dict) -> "dict[str, str]":
    """The ``@attr`` substitution map for one asset — the HDA ``keywords`` dict
    (place/year/style/…) overlaid with the asset's own scalar fields. Mirrors what
    the PDG ``Keywords_From_HDA`` + ``LLM_Prompt_Build`` nodes assemble per item."""
    attrs: dict[str, str] = {}
    kw = inst.parm("keywords")
    if kw is not None:
        try:
            attrs.update({k: str(v) for k, v in kw.eval().items()})
        except Exception:
            pass
    for key, val in asset.items():
        if isinstance(val, (str, int, float)):
            attrs[key] = str(val)
    return attrs


def _expand_prompt(template: str, attrs: "dict[str, str]") -> str:
    """Resolve ``@attr`` placeholders in a prompt template, leaving unknown tokens
    untouched — the same ``re.sub(r"@(\\w+)", …)`` the pipeline uses."""
    return re.sub(r"@(\w+)", lambda m: attrs.get(m.group(1), m.group(0)), template)


def render_asset_preview(inst: hou.Node, asset: dict, cam_preset: str) -> str:
    """Render one asset's isolated clay placeholder from the chosen Asset_Cam.

    Drives the per-asset render rig the way the PDG cook does — ``@name``
    isolation on ``Assets/blast1`` feeding ``Centered`` through ``Asset_OpenGL`` —
    but with literal values so it works in a live (non-PDG) UI render. The ROP's
    ``camera``/``picture`` and the blast group are saved and restored, so the
    batch pipeline stays attribute-driven. Returns the written path.

    Must run on the GUI thread: Houdini cooking/rendering is not thread-safe.
    """
    rop = inst.node("OpenGL/Asset_OpenGL")
    blast = inst.node("Assets/blast1")
    if rop is None or blast is None:
        raise RuntimeError(
            "per-asset render rig missing (OpenGL/Asset_OpenGL or Assets/blast1)")
    cam_node = ASSET_CAMS.get(cam_preset, ASSET_CAMS[DEFAULT_CAM])
    out = asset_input_path(asset_dir_of(asset))
    os.makedirs(os.path.dirname(out), exist_ok=True)

    p_pic, p_cam, p_grp = rop.parm("picture"), rop.parm("camera"), blast.parm("group")
    s_pic, s_cam, s_grp = (p_pic.unexpandedString(), p_cam.unexpandedString(),
                           p_grp.unexpandedString())
    try:
        p_grp.set("@name=%s" % asset.get("name", ""))   # isolate this asset
        p_cam.set("../../%s" % cam_node)
        p_pic.set(out)
        rop.render(verbose=False)
    finally:
        p_pic.set(s_pic)
        p_cam.set(s_cam)
        p_grp.set(s_grp)
    return out


def select_work_item(inst: hou.Node, asset_name: str) -> bool:
    """Make the named asset's work item the TOP network's active selection, so the
    viewport PDG overlay and ``@attr`` references follow the card you click.

    Selection is network-wide by work-item *id* (``setSelectedWorkItem``), so we
    match ``@name`` on the ``Resolver`` (one item per asset) and fall back to any
    other node carrying a matching per-asset item. No-op (returns ``False``) if the
    TOP net hasn't generated work items yet. Returns ``True`` on a match."""
    top = inst.node("Asset_Forge")
    if top is None:
        return False
    res = top.node("Resolver")
    nodes = ([res] if res is not None else []) + \
            [c for c in top.children() if c is not res]
    for nd in nodes:
        try:
            pdgnode = nd.getPDGNode()
        except Exception:
            pdgnode = None
        if pdgnode is None:
            continue
        for w in pdgnode.workItems:
            try:
                if w.stringAttribValue("name") == asset_name:
                    nd.setSelectedWorkItem(w.id)
                    return True
            except Exception:
                continue
    return False


def ensure_work_items(inst: hou.Node) -> int:
    """Populate the per-asset work items so card selection / work-item filtering
    works the moment the UI opens.

    ``generateStaticWorkItems`` yields nothing here — the JSON Input TOP only
    emits items during a *cook* — so we cook the cheap ``load_assets → Resolver``
    branch (a JSON read + the path-authoring python processor; no AI, no render)
    to materialise the ``@name`` items that :func:`select_work_item` matches on.
    No-op if items already exist or ``assets.json`` isn't ready. Returns the
    resulting item count. Must run on the GUI thread (Houdini cooking isn't
    thread-safe)."""
    top = inst.node("Asset_Forge")
    if top is None:
        return 0
    res = top.node("Resolver")
    if res is None:
        return 0
    pdg = res.getPDGNode()
    if pdg is not None and pdg.workItems:
        return len(pdg.workItems)          # already cooked — don't redo the work
    try:
        res.cookWorkItems(block=True)       # cheap branch → safe to block briefly
    except Exception:
        log.exception("ensure_work_items: Resolver branch cook failed")
        return 0
    pdg = res.getPDGNode()
    return len(pdg.workItems) if pdg is not None else 0


# --------------------------------------------------------------------------- #
# Generation worker (runs off the GUI thread)
# --------------------------------------------------------------------------- #
class _GenWorker(QtCore.QObject):
    """Fires N sequential nano_banana calls; emits each result to the GUI."""

    candidate_done = QtCore.Signal(int, str)
    failed = QtCore.Signal(int, str)
    finished = QtCore.Signal()

    def __init__(self, inst_path: str, in_path: str, prompt: str,
                 model: str, count: int) -> None:
        super().__init__()
        self._inst_path = inst_path
        self._in_path = in_path
        self._prompt = prompt
        self._model = model
        self._count = count

    def run(self) -> None:
        import importlib
        import nano_banana_client
        importlib.reload(nano_banana_client)
        inst = hou.node(self._inst_path)
        for i in range(1, self._count + 1):
            out = candidate_path(inst, i)
            try:
                nano_banana_client.generate_image(
                    self._in_path, out, self._prompt,
                    model=self._model or None, verbose=False,
                )
                if os.path.isfile(out):
                    self.candidate_done.emit(i, out)
                else:
                    self.failed.emit(i, "no file produced")
            except Exception as exc:
                self.failed.emit(i, str(exc))
        self.finished.emit()


class _AssetGenWorker(QtCore.QObject):
    """Per-asset batch of nano_banana calls, off the GUI thread.

    Each job is ``(asset_id, asset_dir, in_path, prompt, count)`` — one work item's
    style pass over its pre-rendered ``preview.jpg``. Emits per candidate so the
    owning card refreshes as ``gen_*.png`` land. Same threading split as
    :class:`_GenWorker`: network/file work here, all node cooking stays on the GUI
    thread (the previews are rendered before this worker starts)."""

    candidate_done = QtCore.Signal(str, int, str)   # asset_id, index, out_path
    failed = QtCore.Signal(str, int, str)           # asset_id, index, message
    finished = QtCore.Signal()

    def __init__(self, jobs: "list[tuple]", model: str) -> None:
        super().__init__()
        self._jobs = jobs
        self._model = model

    def run(self) -> None:
        import importlib
        import nano_banana_client
        importlib.reload(nano_banana_client)
        for asset_id, adir, in_path, prompt, count in self._jobs:
            for i in range(1, count + 1):
                out = asset_candidate_path(adir, i)
                try:
                    nano_banana_client.generate_image(
                        in_path, out, prompt,
                        model=self._model or None, verbose=False,
                    )
                    if os.path.isfile(out):
                        self.candidate_done.emit(asset_id, i, out)
                    else:
                        log.error("nano_banana produced no file — asset=%s "
                                  "candidate=%d → %s", asset_id, i, out)
                        self.failed.emit(asset_id, i, "no file produced")
                except Exception as exc:
                    log.exception("nano_banana failed — asset=%s candidate=%d",
                                  asset_id, i)
                    self.failed.emit(asset_id, i, str(exc))
        self.finished.emit()


# --------------------------------------------------------------------------- #
# Bound-widget helpers
# --------------------------------------------------------------------------- #
def _missing_row(label: str, parm: str = "") -> "QtWidgets.QWidget":
    """Placeholder row shown when a bound node/parm can't be found — keeps the
    panel alive (and self-documenting) while nodes are being renamed/rebuilt."""
    row = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lab = QtWidgets.QLabel(label)
    lab.setMinimumWidth(70)
    lay.addWidget(lab)
    miss = QtWidgets.QLabel("— missing: %s" % parm if parm else "— missing")
    miss.setStyleSheet("color:%s;font-style:italic;" % THEME["muted_2"])
    lay.addWidget(miss, 1)
    return row


def _float_slider(node: Optional[hou.Node], parm: str, label: str,
                  lo: float, hi: float, step: float = 0.5) -> "QtWidgets.QWidget":
    """A label + horizontal slider + value readout, bound to a float parm.
    Degrades to a placeholder row if the node/parm is missing."""
    p = node.parm(parm) if node is not None else None
    if p is None:
        return _missing_row(label, parm)
    steps = max(1, int(round((hi - lo) / step)))
    row = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lab = QtWidgets.QLabel(label)
    lab.setMinimumWidth(70)
    lay.addWidget(lab)
    sld = QtWidgets.QSlider(QtCore.Qt.Horizontal)
    sld.setRange(0, steps)
    sld.setValue(int(round((p.eval() - lo) / step)))
    val = QtWidgets.QLabel("%.1f" % p.eval())
    val.setMinimumWidth(48)
    val.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

    def _on(v: int, p=p, lo=lo, step=step, val=val) -> None:
        fv = lo + v * step
        p.set(fv)
        val.setText("%.1f" % fv)

    sld.valueChanged.connect(_on)
    lay.addWidget(sld, 1)
    lay.addWidget(val)
    return row


def _menu_row(node: Optional[hou.Node], parm: str, label: str) -> "QtWidgets.QWidget":
    if node is None or node.parm(parm) is None:
        return _missing_row(label, parm)
    pt = node.parm(parm).parmTemplate()
    combo = QtWidgets.QComboBox()
    combo.addItems(list(pt.menuLabels()))
    val = node.parm(parm).eval()
    combo.setCurrentIndex(val if val < combo.count() else 0)
    combo.currentIndexChanged.connect(lambda idx, p=node.parm(parm): p.set(idx))
    return _labeled(label, combo)


def _labeled(label: str, widget: "QtWidgets.QWidget") -> "QtWidgets.QWidget":
    row = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lab = QtWidgets.QLabel(label)
    lab.setMinimumWidth(90)
    lay.addWidget(lab)
    lay.addWidget(widget, 1)
    return row


def _segmented(options: "list[tuple[str, str]]", current: str,
               on_change) -> "QtWidgets.QWidget":
    """An exclusive button group (e.g. the Layout/Blockout/Generated context).

    ``options`` is ``[(value, label), …]``; ``on_change(value)`` fires on click.
    Styled via the ``#segItem`` QSS rule (accent when ``:checked``).
    """
    row = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    group = QtWidgets.QButtonGroup(row)
    group.setExclusive(True)
    for value, label in options:
        btn = QtWidgets.QPushButton(label)
        btn.setObjectName("segItem")
        btn.setCheckable(True)
        btn.setCursor(QtCore.Qt.PointingHandCursor)
        btn.setChecked(value == current)
        btn.clicked.connect(lambda _=False, v=value: on_change(v))
        group.addButton(btn)
        lay.addWidget(btn)
    lay.addStretch(1)
    return row


class _ClickableLabel(QtWidgets.QLabel):
    """A QLabel that emits ``clicked`` — used for full-size candidate previews."""

    clicked = QtCore.Signal()

    def mousePressEvent(self, ev: "QtGui.QMouseEvent") -> None:
        self.clicked.emit()
        super().mousePressEvent(ev)


class Section(QtWidgets.QWidget):
    """A titled, collapsible container — the building block for UI stages.

    Replaces the old flat ``_divider() + QVBoxLayout`` pattern. Each pipeline
    stage (Distribution, Camera, …, and future per-asset / mesh stages) is one
    ``Section``. Add content with :meth:`addWidget` / :meth:`addLayout`.
    """

    def __init__(self, title: str, collapsed: bool = False,
                 parent: Optional["QtWidgets.QWidget"] = None) -> None:
        super().__init__(parent)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 6, 0, 2)
        outer.setSpacing(2)

        self._toggle = QtWidgets.QToolButton()
        self._toggle.setObjectName("sectionHeader")
        self._toggle.setText(title.upper())
        # Stretch to full width so the title shows beside the arrow (otherwise the
        # tool button collapses to its arrow-only minimum width).
        self._toggle.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                   QtWidgets.QSizePolicy.Fixed)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(not collapsed)
        self._toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self._toggle.setArrowType(
            QtCore.Qt.DownArrow if not collapsed else QtCore.Qt.RightArrow)
        self._toggle.clicked.connect(self._on_toggle)
        outer.addWidget(self._toggle)

        rule = QtWidgets.QFrame()
        rule.setObjectName("sectionRule")
        rule.setFrameShape(QtWidgets.QFrame.HLine)
        rule.setFrameShadow(QtWidgets.QFrame.Sunken)
        outer.addWidget(rule)

        self._body = QtWidgets.QWidget()
        self._content = QtWidgets.QVBoxLayout(self._body)
        self._content.setContentsMargins(0, 4, 0, 4)
        outer.addWidget(self._body)
        self._body.setVisible(not collapsed)

    def _on_toggle(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self._toggle.setArrowType(
            QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)

    def addWidget(self, widget: "QtWidgets.QWidget") -> None:
        self._content.addWidget(widget)

    def addLayout(self, layout: "QtWidgets.QLayout") -> None:
        self._content.addLayout(layout)


# --------------------------------------------------------------------------- #
# Small shared view helpers
# --------------------------------------------------------------------------- #
def _scaled_pixmap(path: str, w: int, h: Optional[int] = None) -> "QtGui.QPixmap":
    """Load ``path`` scaled to width ``w`` (or fitted into ``w×h``)."""
    pix = QtGui.QPixmap(path)
    if pix.isNull():
        return pix
    if h is None:
        return pix.scaledToWidth(w, QtCore.Qt.SmoothTransformation)
    return pix.scaled(w, h, QtCore.Qt.KeepAspectRatio,
                      QtCore.Qt.SmoothTransformation)


def _status_icon(status: str) -> "QtWidgets.QLabel":
    """A tiny coloured glyph: ✓ done · ◐ generating · · idle."""
    glyph, color = {
        "done":        ("✓", THEME["ok"]),
        "in_progress": ("◐", THEME["warn"]),
    }.get(status, ("·", THEME["muted_2"]))
    lab = QtWidgets.QLabel(glyph)
    lab.setStyleSheet("color:%s;font-weight:700;" % color)
    lab.setToolTip(status)
    return lab


def _title_label(text: str) -> "QtWidgets.QLabel":
    """A bold pane title. Houdini's default Qt font uses *pixelSize* (so
    ``pointSize()`` is -1); bumping pointSize there yields a 2pt font — handle
    both unit systems so the title is actually legible."""
    lab = QtWidgets.QLabel(text)
    f = lab.font()
    if f.pointSize() > 0:
        f.setPointSize(f.pointSize() + 3)
    else:
        f.setPixelSize(max(f.pixelSize(), 11) + 4)
    f.setBold(True)
    lab.setFont(f)
    return lab


def _clear_layout(layout: "QtWidgets.QLayout") -> None:
    """Remove and delete every item in a layout (for rebuildable views)."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())


# --------------------------------------------------------------------------- #
# Results Viewer / Picker — general-purpose, reusable from any "see more
# results" affordance. Grid ↔ primary-focus, keyboard nav, set-keeper.
# --------------------------------------------------------------------------- #
class ResultsViewerDialog(QtWidgets.QDialog):
    """Modal results viewer: candidate grid + primary-focus mode.

    Click an image → it fills the view (prev/next + filmstrip); click it again
    or press Esc → back to the grid. ``Set Keeper`` (button or Enter) copies the
    chosen candidate to the asset's ``generated.png`` and records the choice in
    ``data.json``, then fires ``on_keeper()`` so the calling card can refresh.
    """

    GRID_COLS = 4

    def __init__(self, title: str, subtitle: str, candidates: "list[str]",
                 asset_dir: str, on_keeper=None, on_generate_more=None,
                 parent: Optional["QtWidgets.QWidget"] = None) -> None:
        super().__init__(parent)
        self.setObjectName("picker")
        self.setStyleSheet(_STYLESHEET)
        self.setWindowTitle("Results Viewer")
        self.resize(1100, 760)
        self._title = title
        self._subtitle = subtitle
        self._cands = list(candidates)
        self._asset_dir = asset_dir
        self._on_keeper = on_keeper
        self._on_generate_more_cb = on_generate_more
        self._focused: Optional[int] = None
        self._keeper = self._initial_keeper()
        self._build()
        self._show_grid()

    # -- keeper bookkeeping ---------------------------------------------- #
    def _initial_keeper(self) -> Optional[int]:
        name = load_asset_state(self._asset_dir).get("ui", {}).get("keeper")
        if name:
            for i, p in enumerate(self._cands):
                if os.path.basename(p) == name:
                    return i
        return None

    # -- chrome ----------------------------------------------------------- #
    def _build(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = QtWidgets.QWidget()
        top.setStyleSheet("background:#1c1c1c;")
        tl = QtWidgets.QHBoxLayout(top)
        tl.setContentsMargins(18, 10, 18, 10)
        title = QtWidgets.QLabel(
            "<div style='color:#888;font-size:10px;letter-spacing:0.08em'>"
            "RESULTS VIEWER</div>"
            "<div style='font-size:15px;font-weight:600'>%s "
            "<span style='color:#888;font-size:11px'>%s</span></div>"
            % (self._title, self._subtitle))
        tl.addWidget(title)
        tl.addStretch(1)
        self._count = QtWidgets.QLabel("")
        self._count.setStyleSheet("color:#aaa;")
        tl.addWidget(self._count)

        self._back_btn = QtWidgets.QPushButton("‹ Back to grid")
        self._back_btn.clicked.connect(self._show_grid)
        more_btn = QtWidgets.QPushButton("↻ Generate more")
        more_btn.clicked.connect(self._on_generate_more)   # stub
        self._keep_btn = QtWidgets.QPushButton("Set Keeper")
        self._keep_btn.clicked.connect(self._on_set_keeper)
        close_btn = QtWidgets.QPushButton("Close ✕")
        close_btn.clicked.connect(self.accept)
        for b in (self._back_btn, more_btn, self._keep_btn, close_btn):
            tl.addWidget(b)
        root.addWidget(top)

        self._hint = QtWidgets.QLabel("")
        self._hint.setStyleSheet("background:#222;color:#888;padding:6px 18px;")
        root.addWidget(self._hint)

        self._host = QtWidgets.QWidget()
        self._host.setObjectName("pickerCanvas")
        self._host_lay = QtWidgets.QVBoxLayout(self._host)
        self._host_lay.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._host, 1)

    def _refresh_chrome(self) -> None:
        n = len(self._cands)
        kp = "" if self._keeper is None else " · keeper #%d" % (self._keeper + 1)
        self._count.setText("%d candidate(s)%s" % (n, kp))
        focusing = self._focused is not None
        self._back_btn.setVisible(focusing)
        self._hint.setText(
            "click image to return  ·  ← / → navigate  ·  "
            "enter to pick  ·  esc to exit" if focusing
            else "click any image → primary focus")

    # -- grid view -------------------------------------------------------- #
    def _show_grid(self) -> None:
        self._focused = None
        _clear_layout(self._host_lay)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        inner = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(inner)
        grid.setSpacing(14)
        grid.setContentsMargins(18, 18, 18, 18)
        if not self._cands:
            empty = QtWidgets.QLabel("no candidates yet")
            empty.setAlignment(QtCore.Qt.AlignCenter)
            empty.setStyleSheet("color:#888;")
            grid.addWidget(empty, 0, 0)
        for i, path in enumerate(self._cands):
            grid.addWidget(self._grid_cell(i, path),
                           i // self.GRID_COLS, i % self.GRID_COLS)
        scroll.setWidget(inner)
        self._host_lay.addWidget(scroll)
        self._refresh_chrome()

    def _grid_cell(self, index: int, path: str) -> "QtWidgets.QWidget":
        cell = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(cell)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        img = _ClickableLabel()
        img.setObjectName("pickerCell")
        img.setProperty("keeper", index == self._keeper)
        img.setAlignment(QtCore.Qt.AlignCenter)
        img.setMinimumSize(240, 180)
        img.setCursor(QtCore.Qt.PointingHandCursor)
        img.setPixmap(_scaled_pixmap(path, 240, 180))
        img.clicked.connect(lambda _=None, i=index: self._show_focus(i))
        lay.addWidget(img)
        cap = QtWidgets.QLabel(
            "#%d%s" % (index + 1, "  KEEPER" if index == self._keeper else ""))
        cap.setStyleSheet("color:#aaa;font-family:monospace;font-size:10px;")
        lay.addWidget(cap)
        return cell

    # -- focus view ------------------------------------------------------- #
    def _show_focus(self, index: int) -> None:
        self._focused = max(0, min(len(self._cands) - 1, index))
        _clear_layout(self._host_lay)
        if not self._cands:
            return self._show_grid()

        body = QtWidgets.QWidget()
        bl = QtWidgets.QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)

        stage = QtWidgets.QWidget()
        stage.setStyleSheet("background:#1a1a1a;")
        sl = QtWidgets.QHBoxLayout(stage)
        prev = QtWidgets.QPushButton("‹")
        prev.setFixedSize(40, 40)
        prev.setEnabled(self._focused > 0)
        prev.clicked.connect(lambda: self._show_focus(self._focused - 1))
        sl.addWidget(prev)
        big = _ClickableLabel()
        big.setAlignment(QtCore.Qt.AlignCenter)
        big.setCursor(QtCore.Qt.PointingHandCursor)
        big.setToolTip("click to return to grid")
        big.setPixmap(_scaled_pixmap(self._cands[self._focused], 760, 620))
        big.clicked.connect(self._show_grid)
        sl.addWidget(big, 1)
        nxt = QtWidgets.QPushButton("›")
        nxt.setFixedSize(40, 40)
        nxt.setEnabled(self._focused < len(self._cands) - 1)
        nxt.clicked.connect(lambda: self._show_focus(self._focused + 1))
        sl.addWidget(nxt)
        bl.addWidget(stage, 1)

        bl.addWidget(self._filmstrip())
        self._host_lay.addWidget(body)
        self._refresh_chrome()

    def _filmstrip(self) -> "QtWidgets.QWidget":
        strip = QtWidgets.QScrollArea()
        strip.setFixedHeight(86)
        strip.setWidgetResizable(True)
        strip.setFrameShape(QtWidgets.QFrame.NoFrame)
        strip.setStyleSheet("background:#161616;")
        inner = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(inner)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(8)
        for i, path in enumerate(self._cands):
            f = _ClickableLabel()
            f.setObjectName("filmFrame")
            f.setProperty("active", i == self._focused)
            f.setFixedSize(58, 58)
            f.setCursor(QtCore.Qt.PointingHandCursor)
            f.setPixmap(_scaled_pixmap(path, 58, 58))
            f.clicked.connect(lambda _=None, idx=i: self._show_focus(idx))
            row.addWidget(f)
        row.addStretch(1)
        strip.setWidget(inner)
        return strip

    # -- actions ---------------------------------------------------------- #
    def _on_set_keeper(self) -> None:
        idx = self._focused if self._focused is not None else self._keeper
        if idx is None or not self._cands:
            return
        src = self._cands[idx]
        dst = asset_keeper_path(self._asset_dir)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        save_asset_state(self._asset_dir, keeper=os.path.basename(src))
        self._keeper = idx
        if self._on_keeper:
            self._on_keeper()
        # repaint current view to update KEEPER stamps
        (self._show_focus(self._focused) if self._focused is not None
         else self._show_grid())

    def _on_generate_more(self) -> None:
        """Re-roll more candidates for this asset (delegates to the owning
        AssetsWidget, which renders/generates and calls
        :meth:`reload_candidates` as fresh ``gen_*.png`` land)."""
        if self._on_generate_more_cb is None:
            self._hint.setText("Generate more — not available here")
            return
        self._hint.setText("generating…")
        self._on_generate_more_cb()

    def reload_candidates(self) -> None:
        """Re-scan the asset folder for ``gen_*.png`` and repaint, keeping the
        current grid/focus view. Called by the generation worker per candidate."""
        self._cands = asset_candidates(self._asset_dir)
        if self._focused is not None and self._cands:
            self._show_focus(min(self._focused, len(self._cands) - 1))
        else:
            self._show_grid()

    # -- keyboard --------------------------------------------------------- #
    def keyPressEvent(self, ev: "QtGui.QKeyEvent") -> None:
        key = ev.key()
        if self._focused is not None:
            if key == QtCore.Qt.Key_Escape:
                return self._show_grid()
            if key == QtCore.Qt.Key_Left:
                return self._show_focus(self._focused - 1)
            if key == QtCore.Qt.Key_Right:
                return self._show_focus(self._focused + 1)
            if key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                return self._on_set_keeper()
        elif key == QtCore.Qt.Key_Escape:
            return self.accept()
        super().keyPressEvent(ev)


# --------------------------------------------------------------------------- #
# Asset card — one per entry in assets.json (the per-asset stage).
# CAMERA_PRESETS / ASSET_CAMS live up top (next to the per-asset path helpers).
# --------------------------------------------------------------------------- #
class AssetCard(QtWidgets.QFrame):
    """A stacked asset card: preview thumb · name/path/status · controls · results.

    Reads/writes per-asset UI state (camera, variations, include, keeper) through
    the asset's ``data.json``. Clicking the card body selects the asset; the
    results strip / expand opens the shared :class:`ResultsViewerDialog`.
    """

    selected = QtCore.Signal(str)        # asset id
    camera_changed = QtCore.Signal(str, str)  # asset id, camera preset

    _RESULT_CAP = 8

    def __init__(self, asset: dict, mode: str, active: bool,
                 on_open_picker, parent: Optional["QtWidgets.QWidget"] = None) -> None:
        super().__init__(parent)
        self._asset = asset
        self._mode = mode
        self._dir = asset_dir_of(asset)
        self._state = load_asset_state(self._dir).get("ui", {})
        self._on_open_picker = on_open_picker
        self.setObjectName("assetCard")
        self.setProperty("active", active)
        self.setProperty("included", bool(self._state.get("include", True)))
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self._build()

    # -- status helpers --------------------------------------------------- #
    def _status(self) -> str:
        return "done" if os.path.isfile(asset_keeper_path(self._dir)) else "idle"

    def _status_text(self) -> str:
        n = len(asset_candidates(self._dir))
        var = int(self._state.get("variations", self._asset.get("variations", 4)))
        if self._status() == "done":
            return "%d / %d ready" % (n or var, var)
        return "0 / %d ready" % var

    # -- build ------------------------------------------------------------ #
    def _build(self) -> None:
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(12, 10, 12, 10)
        grid.setHorizontalSpacing(14)

        # Col 0 — preview thumb (clickable → select)
        self._thumb = _ClickableLabel()
        self._thumb.setObjectName("assetThumb")
        self._thumb.setFixedSize(96, 70)
        self._thumb.setAlignment(QtCore.Qt.AlignCenter)
        self._thumb.clicked.connect(self._emit_select)
        grid.addWidget(self._thumb, 0, 0)

        # Col 1 — name / path / status meta
        info = QtWidgets.QWidget()
        il = QtWidgets.QVBoxLayout(info)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(2)
        name_row = QtWidgets.QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        self._status_icon = _status_icon(self._status())
        name_row.addWidget(self._status_icon)
        name = QtWidgets.QLabel(self._asset.get("name", self._asset.get("id", "?")))
        name.setObjectName("cardName")
        name_row.addWidget(name)
        name_row.addStretch(1)
        il.addLayout(name_row)
        path = QtWidgets.QLabel("/obj/assets/%s" % self._asset.get("name", ""))
        path.setObjectName("cardPath")
        il.addWidget(path)
        self._meta = QtWidgets.QLabel(self._meta_text())
        self._meta.setObjectName("cardMeta")
        il.addWidget(self._meta)
        grid.addWidget(info, 0, 1)

        # Col 2 — controls (camera / variations / include)
        ctl = QtWidgets.QWidget()
        cl = QtWidgets.QHBoxLayout(ctl)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)
        self._cam = QtWidgets.QComboBox()
        self._cam.addItems(CAMERA_PRESETS)
        cam_val = self._state.get("camera", CAMERA_PRESETS[0])
        if cam_val in CAMERA_PRESETS:
            self._cam.setCurrentText(cam_val)
        self._cam.currentTextChanged.connect(self._on_cam_change)
        cl.addWidget(_labeled_compact("CAMERA", self._cam))
        self._var = QtWidgets.QSpinBox()
        self._var.setRange(1, 8)
        self._var.setValue(int(self._state.get(
            "variations", self._asset.get("variations", 4))))
        self._var.valueChanged.connect(lambda v: self._save(variations=v))
        cl.addWidget(_labeled_compact("VAR", self._var))
        self._incl = QtWidgets.QCheckBox("incl.")
        self._incl.setChecked(bool(self._state.get("include", True)))
        self._incl.stateChanged.connect(self._on_include)
        cl.addWidget(self._incl)
        grid.addWidget(ctl, 0, 2)

        # Col 3 — results strip + expand
        res = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(res)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(QtWidgets.QLabel("RESULTS"))
        expand = QtWidgets.QPushButton("expand ⤢")
        expand.setFlat(True)
        expand.setStyleSheet("color:%s;border:none;" % THEME["accent"])
        expand.setCursor(QtCore.Qt.PointingHandCursor)
        expand.clicked.connect(self._open_picker)
        head.addStretch(1)
        head.addWidget(expand)
        rl.addLayout(head)
        self._results_host = QtWidgets.QWidget()
        self._results_lay = QtWidgets.QHBoxLayout(self._results_host)
        self._results_lay.setContentsMargins(0, 0, 0, 0)
        self._results_lay.setSpacing(4)
        rl.addWidget(self._results_host)
        grid.addWidget(res, 0, 3)

        grid.setColumnStretch(1, 1)
        self._refresh_thumb()
        self._refresh_results()

    def _meta_text(self) -> str:
        return "showing: %s   ·   %s" % (self._mode, self._status_text())

    # -- dynamic bits ----------------------------------------------------- #
    def _refresh_thumb(self) -> None:
        path = asset_thumb_path(self._dir, self._mode)
        if path:
            self._thumb.setPixmap(_scaled_pixmap(path, 96, 70))
            self._thumb.setText("")
        else:
            self._thumb.setPixmap(QtGui.QPixmap())
            self._thumb.setText("no render")
            self._thumb.setStyleSheet(
                "color:%s;font-size:9px;" % THEME["text_muted"])

    def _refresh_results(self) -> None:
        _clear_layout(self._results_lay)
        cands = asset_candidates(self._dir)[: self._RESULT_CAP]
        keeper_name = self._state.get("keeper")
        if not cands:
            ph = QtWidgets.QLabel("—")
            ph.setStyleSheet("color:%s;" % THEME["muted_2"])
            self._results_lay.addWidget(ph)
        for path in cands:
            t = _ClickableLabel()
            t.setObjectName("resultThumb")
            t.setProperty("keeper", os.path.basename(path) == keeper_name)
            t.setFixedSize(40, 40)
            t.setCursor(QtCore.Qt.PointingHandCursor)
            t.setPixmap(_scaled_pixmap(path, 40, 40))
            t.clicked.connect(lambda _=None: self._open_picker())
            self._results_lay.addWidget(t)
        self._results_lay.addStretch(1)

    def refresh(self) -> None:
        """Re-read state from disk and repaint dynamic bits (thumb/status/results)."""
        self._state = load_asset_state(self._dir).get("ui", {})
        self._meta.setText(self._meta_text())
        self._refresh_thumb()
        self._refresh_results()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._meta.setText(self._meta_text())
        self._refresh_thumb()

    def set_active(self, active: bool) -> None:
        self.setProperty("active", active)
        self._restyle()

    # -- actions ---------------------------------------------------------- #
    def _save(self, **updates) -> None:
        self._state.update(updates)
        save_asset_state(self._dir, **updates)

    def _on_include(self, state: int) -> None:
        # Read the widget directly — comparing the signal's arg against
        # QtCore.Qt.Checked is unreliable under PySide6's enum system (int vs
        # Qt.CheckState), which silently saved include=False even when ticked.
        on = self._incl.isChecked()
        self._save(include=on)
        self.setProperty("included", on)
        self._restyle()

    def _restyle(self) -> None:
        # Re-evaluate property-based QSS after a property changes.
        self.style().unpolish(self)
        self.style().polish(self)

    def _on_cam_change(self, v: str) -> None:
        """Persist the camera preset, then let the stage activate this card and
        point the Scene Viewer at the chosen Asset_Cam."""
        self._save(camera=v)
        self.camera_changed.emit(self._asset.get("id", ""), v)

    def asset_id(self) -> str:
        return self._asset.get("id", "")

    def asset_name(self) -> str:
        return self._asset.get("name", "")

    def camera_preset(self) -> str:
        return self._state.get("camera", DEFAULT_CAM)

    def _emit_select(self, *_) -> None:
        self.selected.emit(self._asset.get("id", ""))

    def _open_picker(self) -> None:
        if self._on_open_picker:
            self._on_open_picker(self._asset, self.refresh)

    def mousePressEvent(self, ev: "QtGui.QMouseEvent") -> None:
        self._emit_select()
        super().mousePressEvent(ev)


def _labeled_compact(label: str, widget: "QtWidgets.QWidget") -> "QtWidgets.QWidget":
    """A tiny stacked caption-over-control (the card's CAMERA/VAR columns)."""
    box = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(1)
    cap = QtWidgets.QLabel(label)
    cap.setStyleSheet("color:%s;font-size:9px;letter-spacing:0.06em;"
                      % THEME["text_muted"])
    lay.addWidget(cap)
    lay.addWidget(widget)
    return box


# --------------------------------------------------------------------------- #
# Layout panel — framing, preview render, layout generation.
# A standalone Python Panel widget (dock it where you like, e.g. top-right).
# --------------------------------------------------------------------------- #
class LayoutControlsWidget(QtWidgets.QWidget):
    """The Layout stage: Distribution / Camera / Preview / Generation."""

    def __init__(self, inst: hou.Node,
                 parent: Optional["QtWidgets.QWidget"] = None) -> None:
        super().__init__(parent)
        self._inst = inst
        self._thread: Optional[QtCore.QThread] = None
        self._worker: Optional[_GenWorker] = None
        self._candidates: list[str] = []

        # Show the Layout geo in the viewport (drives Display/switch1).
        _set_display_context(inst, "layout")

        self._build()

    def _build(self) -> None:
        """Assemble the Layout stage. Each ``_build_*`` returns a self-contained
        ``Section`` mapping 1:1 to a design section, so re-skinning is a matter of
        rebuilding the widget tree — the parm bindings + worker logic stay put."""
        self.setStyleSheet(_STYLESHEET)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        inner = QtWidgets.QWidget()
        scroll.setWidget(inner)
        root = QtWidgets.QVBoxLayout(inner)

        root.addWidget(_title_label("LAYOUT"))
        root.addWidget(self._build_distribution())
        root.addWidget(self._build_camera())
        root.addWidget(self._build_preview())
        root.addWidget(self._build_generation())

        self._status = QtWidgets.QLabel("")
        self._status.setObjectName("status")
        self._status.setWordWrap(True)
        root.addWidget(self._status)
        root.addStretch(1)

        self._refresh_preview()
        self._load_existing_candidates()

    def _build_distribution(self) -> "Section":
        ad = self._inst.node("Layout/align_and_distribute1")
        sec = Section("Distribution")
        sec.addWidget(_float_slider(ad, "spacing", "Spacing", 0.0, 10.0, 0.1))
        sec.addWidget(_float_slider(ad, "seed", "Seed", 0.0, 10.0, 0.1))
        sec.addWidget(_menu_row(ad, "layout", "Layout"))
        sec.addWidget(_menu_row(ad, "orientation", "Plane"))
        sec.addWidget(_menu_row(ad, "justifyx", "Justify X"))
        sec.addWidget(_menu_row(ad, "justifyy", "Justify Y"))
        sec.addWidget(_menu_row(ad, "justifyz", "Justify Z"))
        return sec

    def _build_camera(self) -> "Section":
        cam = _layout_cam(self._inst)             # ortho layout cam
        ln = self._inst.node("Layout_Null")       # orbit pivot
        sec = Section("Camera")
        sec.addWidget(_float_slider(ln, "ry", "Rotation", -180.0, 180.0, 1.0))
        sec.addWidget(_float_slider(ln, "rx", "Pitch", -90.0, 90.0, 0.5))
        sec.addWidget(_float_slider(ln, "ty", "Height", -50.0, 50.0, 0.5))
        sec.addWidget(_float_slider(cam, "orthowidth", "Distance", 1.0, 150.0, 0.5))
        return sec

    def _build_preview(self) -> "Section":
        sec = Section("Preview render")
        self._render_btn = QtWidgets.QPushButton("Render Layout Preview")
        self._render_btn.clicked.connect(self._on_render)
        sec.addWidget(self._render_btn)

        self._preview_label = QtWidgets.QLabel("no preview yet")
        self._preview_label.setObjectName("previewLabel")
        self._preview_label.setAlignment(QtCore.Qt.AlignCenter)
        self._preview_label.setMinimumHeight(220)
        sec.addWidget(self._preview_label)
        return sec

    def _build_generation(self) -> "Section":
        sec = Section("Layout generation")
        self._prompt = QtWidgets.QPlainTextEdit()
        self._prompt.setPlaceholderText("Layout style prompt…")
        # Bind two-way to the HDA layout_prompt parm so it persists in the HDA.
        lp = self._inst.parm("layout_prompt")
        if lp is not None:
            self._prompt.setPlainText(lp.unexpandedString())
            self._prompt.textChanged.connect(
                lambda p=lp: p.set(self._prompt.toPlainText()))
        self._prompt.setMaximumHeight(90)
        sec.addWidget(self._prompt)

        ctl = QtWidgets.QHBoxLayout()
        ctl.addWidget(QtWidgets.QLabel("Variations"))
        self._count = QtWidgets.QSpinBox()
        self._count.setRange(1, 8)
        self._count.setValue(4)
        ctl.addWidget(self._count)
        self._gen_btn = QtWidgets.QPushButton("Generate")
        self._gen_btn.clicked.connect(self._on_generate)
        ctl.addWidget(self._gen_btn, 1)
        sec.addLayout(ctl)

        self._gallery = QtWidgets.QGridLayout()
        self._gallery.setSpacing(6)
        gallery_box = QtWidgets.QWidget()
        gallery_box.setLayout(self._gallery)
        sec.addWidget(gallery_box)
        return sec

    # -- preview render --------------------------------------------------- #
    def _on_render(self) -> None:
        rop = self._inst.node("OpenGL/Layout_OpenGL")
        if rop is None:
            self._status.setText("render ROP not found (OpenGL/Layout_OpenGL)")
            return
        out = preview_path(self._inst)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        picture = rop.parm("picture")
        saved = picture.unexpandedString()
        self._status.setText("rendering…")
        QtWidgets.QApplication.processEvents()
        try:
            picture.set(out)
            rop.render(verbose=False)
        finally:
            picture.set(saved)
        self._refresh_preview()
        self._status.setText("rendered %s" % os.path.basename(out))

    def _img_width(self) -> int:
        """Shared display width for the render preview and candidate previews."""
        return max(self._preview_label.width(), 420)

    def _refresh_preview(self) -> None:
        path = preview_path(self._inst)
        if os.path.isfile(path):
            pix = QtGui.QPixmap(path).scaledToWidth(
                self._img_width(), QtCore.Qt.SmoothTransformation)
            self._preview_label.setPixmap(pix)
        else:
            self._preview_label.setText("no preview yet")

    # -- generation ------------------------------------------------------- #
    def _on_generate(self) -> None:
        in_path = preview_path(self._inst)
        if not os.path.isfile(in_path):
            self._status.setText("render a layout preview first")
            return
        if self._thread is not None:
            return
        self._clear_gallery()
        prompt = self._prompt.toPlainText().strip()
        model = (self._inst.parm("image_model").eval()
                 if self._inst.parm("image_model") else "")
        n = self._count.value()
        self._gen_btn.setEnabled(False)
        self._status.setText("generating 0/%d…" % n)

        self._thread = QtCore.QThread()
        self._worker = _GenWorker(self._inst.path(), in_path, prompt, model, n)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.candidate_done.connect(self._on_candidate)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_gen_finished)
        self._thread.start()

    _GALLERY_COLS = 1

    def _add_thumb(self, path: str) -> None:
        """Append a full-size clickable preview (matches the render preview)."""
        lab = _ClickableLabel()
        lab.setObjectName("thumb")
        lab.setAlignment(QtCore.Qt.AlignCenter)
        lab.setCursor(QtCore.Qt.PointingHandCursor)
        lab.setToolTip("Click to select → layout_generated.png")
        lab.setPixmap(QtGui.QPixmap(path).scaledToWidth(
            self._img_width(), QtCore.Qt.SmoothTransformation))
        lab.clicked.connect(lambda p=path: self._select(p))
        slot = len(self._candidates)
        self._candidates.append(path)
        self._gallery.addWidget(lab, slot // self._GALLERY_COLS,
                                slot % self._GALLERY_COLS)

    def _load_existing_candidates(self) -> None:
        """Repopulate the gallery from layout_gen_*.png on disk (persists across
        sessions — open the panel and prior generations are already there)."""
        d = layout_dir(self._inst)
        if not os.path.isdir(d):
            return
        files = sorted(f for f in os.listdir(d)
                       if f.startswith("layout_gen_") and f.lower().endswith(".png"))
        for f in files:
            self._add_thumb(os.path.join(d, f))
        if files:
            self._status.setText("%d existing generation(s) — click one to keep"
                                 % len(files))

    def _on_candidate(self, index: int, path: str) -> None:
        self._add_thumb(path)
        self._status.setText("generating %d/%d…"
                             % (len(self._candidates), self._count.value()))

    def _on_failed(self, index: int, msg: str) -> None:
        self._status.setText("gen %d failed: %s" % (index, msg))

    def _on_gen_finished(self) -> None:
        self._thread.quit()
        self._thread.wait()
        self._thread = None
        self._worker = None
        self._gen_btn.setEnabled(True)
        self._status.setText("done — %d candidate(s); click one to keep"
                             % len(self._candidates))

    def _select(self, path: str) -> None:
        dst = generated_path(self._inst)
        shutil.copyfile(path, dst)
        self._status.setText("selected → %s" % os.path.basename(dst))

    def _clear_gallery(self) -> None:
        self._candidates = []
        while self._gallery.count():
            item = self._gallery.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()


# --------------------------------------------------------------------------- #
# Assets panel — the per-asset stage: context switch + shared prompt + cards.
# A standalone Python Panel widget (dock it where you like, e.g. bottom).
# --------------------------------------------------------------------------- #
class AssetsWidget(QtWidgets.QWidget):
    """The per-asset stage: a Layout/Blockout/Generated context switch over a
    shared prompt bar and a stack of asset cards."""

    def __init__(self, inst: hou.Node,
                 parent: Optional["QtWidgets.QWidget"] = None) -> None:
        super().__init__(parent)
        self._inst = inst
        self._mode = "layout"                       # layout | blockout | generated
        self._cards: list[AssetCard] = []
        self._active_asset_id: Optional[str] = None
        self._gen_thread: Optional[QtCore.QThread] = None
        self._gen_worker: Optional[_AssetGenWorker] = None
        self._active_dialog: Optional["ResultsViewerDialog"] = None
        self._gen_total = 0
        self._gen_done = 0
        self._log_path = _attach_log_file(inst)
        self._build()

    def _build(self) -> None:
        self.setStyleSheet(_STYLESHEET)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        # Context switch (drives card thumbnails; viewport-drive is a TODO).
        header = QtWidgets.QHBoxLayout()
        header.addWidget(QtWidgets.QLabel("CONTEXT"))
        self._context_widget = _segmented(
            [("layout", "Layout"), ("blockout", "Blockout"),
             ("generated", "Generated")],
            self._mode, self._on_context_change)
        header.addWidget(self._context_widget)
        header.addStretch(1)
        outer.addLayout(header)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll, 1)
        inner = QtWidgets.QWidget()
        scroll.setWidget(inner)
        root = QtWidgets.QVBoxLayout(inner)

        root.addWidget(_title_label("ASSETS"))
        root.addWidget(self._build_shared_prompt_bar())

        self._cards_host = QtWidgets.QWidget()
        self._cards_lay = QtWidgets.QVBoxLayout(self._cards_host)
        self._cards_lay.setContentsMargins(0, 8, 0, 0)
        self._cards_lay.setSpacing(8)
        root.addWidget(self._cards_host)
        root.addStretch(1)

        self._populate_cards()

    def _build_shared_prompt_bar(self) -> "QtWidgets.QWidget":
        box = QtWidgets.QFrame()
        box.setObjectName("assetCard")
        lay = QtWidgets.QVBoxLayout(box)
        cap = QtWidgets.QLabel("SHARED PROMPT · APPLIED TO ALL ASSETS")
        cap.setStyleSheet("color:%s;font-size:10px;letter-spacing:0.06em;"
                          % THEME["text_muted"])
        lay.addWidget(cap)

        # Two-way bind to the HDA prompt_template parm (the pipeline's per-asset
        # image-gen prompt = "applied to all assets").
        self._shared_prompt = QtWidgets.QPlainTextEdit()
        self._shared_prompt.setPlaceholderText("Shared per-asset style prompt…")
        self._shared_prompt.setMaximumHeight(60)
        pt = self._inst.parm("prompt_template")
        if pt is not None:
            self._shared_prompt.setPlainText(pt.unexpandedString())
            self._shared_prompt.textChanged.connect(
                lambda p=pt: p.set(self._shared_prompt.toPlainText()))
        lay.addWidget(self._shared_prompt)

        # Two-step flow over the *included* assets: render the clay previews from
        # each card's Asset_Cam (fills the card's left thumb), then style-pass them
        # through nano_banana (fills the card's right results strip).
        row = QtWidgets.QHBoxLayout()
        self._render_btn = QtWidgets.QPushButton("Render Preview")
        self._render_btn.clicked.connect(self._on_render_preview)
        self._gen_sel_btn = QtWidgets.QPushButton("Generate Selected")
        self._gen_sel_btn.clicked.connect(self._on_generate_selected)
        row.addWidget(self._render_btn)
        row.addWidget(self._gen_sel_btn)
        row.addStretch(1)
        self._assets_status = QtWidgets.QLabel(self._status_summary())
        self._assets_status.setObjectName("status")
        row.addWidget(self._assets_status)
        lay.addLayout(row)
        return box

    def _populate_cards(self) -> None:
        _clear_layout(self._cards_lay)
        self._cards = []
        assets = load_assets(self._inst)
        if not assets:
            msg = QtWidgets.QLabel(
                "No assets.json found in\n%s" % layout_dir(self._inst))
            msg.setWordWrap(True)
            msg.setStyleSheet("color:%s;" % THEME["text_muted"])
            self._cards_lay.addWidget(msg)
            return
        for a in assets:
            card = AssetCard(a, self._mode,
                             a.get("id") == self._active_asset_id,
                             self._open_asset_picker)
            card.selected.connect(self._on_select_asset)
            card.camera_changed.connect(self._on_card_camera)
            self._cards_lay.addWidget(card)
            self._cards.append(card)

        # On launch, activate the first asset so its work item is filtered and the
        # viewport frames its saved camera (mirrors clicking the first card).
        if self._cards and self._active_asset_id is None:
            self._on_select_asset(self._cards[0].asset_id())

    def _status_summary(self) -> str:
        assets = load_assets(self._inst)
        done = sum(1 for a in assets
                   if os.path.isfile(asset_keeper_path(asset_dir_of(a))))
        return "%d done · %d idle" % (done, len(assets) - done)

    # -- handlers --------------------------------------------------------- #
    def _set_context(self, mode: str) -> None:
        """Switch context programmatically *and* sync the segmented buttons
        (used to jump to Blockout after a preview render so the fresh clay shows)."""
        self._on_context_change(mode)
        for btn in self._context_widget.findChildren(QtWidgets.QPushButton):
            btn.setChecked(btn.text().lower() == mode)

    def _on_context_change(self, mode: str) -> None:
        self._mode = mode
        for c in self._cards:
            c.set_mode(mode)
        self._drive_context_geo(mode)
        # Layout context frames the whole scene through the ortho layout cam;
        # asset contexts frame the active asset through its saved camera.
        if mode == "layout":
            _drive_viewport_camera(_layout_cam(self._inst))
        elif self._active_asset_id is not None:
            card = self._card_by_id(self._active_asset_id)
            if card is not None:
                self._drive_camera(card.camera_preset())

    def _drive_context_geo(self, mode: str) -> None:
        """Show the geo matching the context (Layout/Blockout/Generated) by
        driving the HDA's ``display_node`` parm → ``Display/switch1``."""
        _set_display_context(self._inst, mode)

    def _on_select_asset(self, asset_id: str) -> None:
        self._active_asset_id = asset_id
        for c in self._cards:
            c.set_active(c.asset_id() == asset_id)
        # Drive the TOP network's active work item to the selected asset.
        card = self._card_by_id(asset_id)
        if card is not None:
            try:
                select_work_item(self._inst, card.asset_name())
            except Exception:
                pass
            # Frame the asset through its saved camera — but only in an asset
            # context; layout context keeps the ortho overview (and avoids a
            # launch-time race with the layout-cam pin in launch()).
            if self._mode != "layout":
                self._drive_camera(card.camera_preset())

    def _on_card_camera(self, asset_id: str, preset: str) -> None:
        """A card's CAMERA dropdown changed. If that card isn't active, select it
        (which also re-points the viewport via its now-saved preset); otherwise
        just swing the live viewport to the new camera."""
        if asset_id != self._active_asset_id:
            self._on_select_asset(asset_id)
        else:
            self._drive_camera(preset)

    def _drive_camera(self, preset: str) -> None:
        """Point the Scene Viewer at this instance's Asset_Cam for ``preset``."""
        cam_node = self._inst.node(ASSET_CAMS.get(preset, ASSET_CAMS[DEFAULT_CAM]))
        _drive_viewport_camera(cam_node)

    def _open_asset_picker(self, asset: dict, on_keeper) -> None:
        adir = asset_dir_of(asset)
        name = asset.get("name", asset.get("id", "?"))
        cam = load_asset_state(adir).get("ui", {}).get("camera", "—")
        sub = "/obj/assets/%s · cam: %s" % (name, cam)
        dlg = ResultsViewerDialog(
            name, sub, asset_candidates(adir), adir,
            on_keeper=lambda: (on_keeper(), self._refresh_assets_status()),
            on_generate_more=lambda a=asset: self._start_generation([a]),
            parent=self)
        # Tracked so candidate signals can live-refresh the open picker.
        self._active_dialog = dlg
        try:
            dlg.exec()
        finally:
            self._active_dialog = None

    def _refresh_assets_status(self) -> None:
        if hasattr(self, "_assets_status"):
            self._assets_status.setText(self._status_summary())

    # -- selection / lookup helpers -------------------------------------- #
    def _included_assets(self) -> "list[dict]":
        """Assets with their card's ``incl.`` checkbox on (default on)."""
        out = []
        for a in load_assets(self._inst):
            ui = load_asset_state(asset_dir_of(a)).get("ui", {})
            if ui.get("include", True):
                out.append(a)
        return out

    def _card_by_id(self, asset_id: str) -> Optional["AssetCard"]:
        for c in self._cards:
            if c.asset_id() == asset_id:
                return c
        return None

    def _set_busy(self, busy: bool) -> None:
        for b in (getattr(self, "_render_btn", None),
                  getattr(self, "_gen_sel_btn", None)):
            if b is not None:
                b.setEnabled(not busy)

    # -- step 1: render the clay previews (Render_Previews TOP node) ------ #
    def _on_render_preview(self) -> None:
        """Cook the ``Render_Previews`` TOP node of this HDA instance, which
        renders every asset's clay preview through PDG. Resolved relative to the
        instance (multi-instance safe) — never a hardcoded ``/obj/...`` path.

        Non-blocking: PDG cooks in the background so the GUI stays responsive; a
        QTimer polls the graph context and refreshes the cards on completion."""
        if self._gen_thread is not None or getattr(self, "_cook_timer", None):
            return
        node = self._inst.node("Asset_Forge/Render_Previews")
        if node is None:
            log.error("Render Preview: Render_Previews TOP node not found under %s",
                      self._inst.path())
            self._assets_status.setText("Render_Previews TOP node not found")
            return
        self._set_busy(True)
        self._assets_status.setText("cooking Render_Previews…")
        QtWidgets.QApplication.processEvents()
        try:
            node.cookWorkItems(block=False)
        except Exception as exc:
            self._set_busy(False)
            log.exception("Render Preview: cook failed")
            self._assets_status.setText("cook failed: %s" % exc)
            return
        self._poll_cook(node.getPDGGraphContext())

    def _poll_cook(self, gc) -> None:
        """Watch a PDG graph context cook; refresh cards + jump to Blockout when
        it finishes. Uses a GUI-thread QTimer (PDG events fire off-thread)."""
        self._cook_timer = QtCore.QTimer(self)
        self._cook_timer.setInterval(400)
        state = {"started": bool(gc.cooking), "idle": 0}

        def _poll() -> None:
            if gc.cooking:
                state["started"] = True
                return
            # Give the cook a few ticks to spin up before declaring it done.
            if not state["started"]:
                state["idle"] += 1
                if state["idle"] < 8:
                    return
            self._cook_timer.stop()
            self._cook_timer = None
            self._set_busy(False)
            for c in self._cards:
                c.refresh()
            # Jump to Blockout so the freshly rendered clay previews are visible.
            self._set_context("blockout")
            self._assets_status.setText("Render_Previews cook complete")

        self._cook_timer.timeout.connect(_poll)
        self._cook_timer.start()

    # -- step 2: style-pass the previews (nano_banana) ------------------- #
    def _on_generate_selected(self) -> None:
        self._start_generation(self._included_assets())

    def _start_generation(self, assets: "list[dict]") -> None:
        """Run N nano_banana variations per asset off the GUI thread, using each
        asset's already-rendered ``preview.jpg`` as the style-ref input."""
        if self._gen_thread is not None:
            return
        tmpl = self._shared_prompt.toPlainText()
        model = (self._inst.parm("image_model").eval()
                 if self._inst.parm("image_model") else "")
        jobs, skipped = [], 0
        for a in assets:
            adir = asset_dir_of(a)
            in_path = asset_input_path(adir)
            if not os.path.isfile(in_path):
                skipped += 1
                continue
            ui = load_asset_state(adir).get("ui", {})
            count = int(ui.get("variations", a.get("variations", 4)))
            prompt = _expand_prompt(tmpl, _attr_map(self._inst, a))
            jobs.append((a.get("id", ""), adir, in_path, prompt, count))
        if not jobs:
            if not assets:
                why = "no assets have 'incl.' ticked"
            else:
                why = ("%d included asset(s), but none have a preview.jpg yet "
                       "— Render Preview first" % len(assets))
            msg = "nothing to generate — " + why
            log.warning("Generate Selected: %s", msg)
            self._assets_status.setText(msg)
            return
        self._gen_total = sum(j[4] for j in jobs)
        self._gen_done = 0
        self._set_busy(True)
        log.info("Generate Selected: %d asset(s), %d image(s)%s",
                 len(jobs), self._gen_total,
                 " (skipped %d w/o preview)" % skipped if skipped else "")
        msg = "generating 0/%d…" % self._gen_total
        if skipped:
            msg += "  (skipped %d w/o preview)" % skipped
        self._assets_status.setText(msg)

        self._gen_thread = QtCore.QThread()
        self._gen_worker = _AssetGenWorker(jobs, model)
        self._gen_worker.moveToThread(self._gen_thread)
        self._gen_thread.started.connect(self._gen_worker.run)
        self._gen_worker.candidate_done.connect(self._on_asset_candidate)
        self._gen_worker.failed.connect(self._on_asset_failed)
        self._gen_worker.finished.connect(self._on_gen_finished)
        self._gen_thread.start()

    def _on_asset_candidate(self, asset_id: str, index: int, path: str) -> None:
        self._gen_done += 1
        self._assets_status.setText(
            "generating %d/%d…" % (self._gen_done, self._gen_total))
        card = self._card_by_id(asset_id)
        if card is not None:
            card.refresh()
        if self._active_dialog is not None:
            self._active_dialog.reload_candidates()

    def _on_asset_failed(self, asset_id: str, index: int, msg: str) -> None:
        self._gen_done += 1
        log.error("generation failed — asset=%s candidate=%d: %s",
                  asset_id, index, msg)
        self._assets_status.setText("gen failed (%s #%d): %s" % (asset_id, index, msg))

    def _on_gen_finished(self) -> None:
        self._gen_thread.quit()
        self._gen_thread.wait()
        self._gen_thread = None
        self._gen_worker = None
        self._set_busy(False)
        log.info("Generate Selected finished — %s", self._status_summary())
        self._assets_status.setText("done — " + self._status_summary())


# --------------------------------------------------------------------------- #
# Python Panel entries + floating-panel launch
# --------------------------------------------------------------------------- #
def _resolve_instance() -> hou.Node:
    """The instance a freshly-built panel should drive (last launched, or auto)."""
    inst = hou.node(_ACTIVE_INSTANCE) if _ACTIVE_INSTANCE else None
    return inst if inst is not None else find_instance(None)


def create_layout_interface() -> "QtWidgets.QWidget":
    """Python Panel factory for the Layout stage (interface ``asset_forge_layout``)."""
    return LayoutControlsWidget(_resolve_instance())


def create_assets_interface() -> "QtWidgets.QWidget":
    """Python Panel factory for the Assets stage (interface ``asset_forge_assets``)."""
    return AssetsWidget(_resolve_instance())


def createInterface() -> "QtWidgets.QWidget":
    """Deprecated back-compat shim for the old combined ``asset_forge`` interface
    (now split into ``asset_forge_layout`` + ``asset_forge_assets``). Kept so any
    leftover pane tab from before the split doesn't error; returns the Assets
    stage. The stale interface clears on the next Houdini restart."""
    return create_assets_interface()


def _ensure_interfaces_installed() -> None:
    installed = hou.pypanel.interfaces()
    if (LAYOUT_INTERFACE not in installed or ASSETS_INTERFACE not in installed) \
            and os.path.isfile(PYPANEL_FILE):
        hou.pypanel.installFile(PYPANEL_FILE)


def _set_python_panel(pane, interface_name: str) -> None:
    """Turn a pane into a Python Panel showing ``interface_name`` (a fresh split
    inherits a Scene Viewer tab — drop it and surface the Python Panel)."""
    pp = pane.createTab(hou.paneTabType.PythonPanel)
    iface = hou.pypanel.interfaceByName(interface_name)
    if iface is not None:
        pp.setActiveInterface(iface)
    for t in list(pane.tabs()):
        if t.type() == hou.paneTabType.SceneViewer:
            try:
                t.close()
            except Exception:
                pass
    try:
        pp.setIsCurrentTab()
    except Exception:
        pass


def launch(node: Optional[hou.Node] = None) -> "hou.FloatingPanel":
    """Open a floating panel with three panes: a top row split into
    Scene Viewer (left) | Asset Forge · Layout (right), over a full-width
    Asset Forge · Assets pane at the bottom. The two stages are also installed
    as standalone Python Panel interfaces, so you can re-dock them anywhere."""
    global _ACTIVE_INSTANCE
    inst = find_instance(node)
    _ACTIVE_INSTANCE = inst.path()
    # Materialise per-asset work items up front so clicking a card can drive the
    # viewport's work-item filtering — without them setSelectedWorkItem is a no-op.
    ensure_work_items(inst)
    _ensure_interfaces_installed()

    # Don't stack panels on repeated launches.
    for fp in hou.ui.floatingPanels():
        if fp.name() in ("Asset Forge", "Asset_Forge"):
            try:
                fp.close()
            except Exception:
                pass

    desk = hou.ui.curDesktop()
    panel = desk.createFloatingPanel(
        hou.paneTabType.SceneViewer, (), (1600, 1000))

    # Split the whole panel vertically first → full-width Assets pane at bottom.
    top = panel.panes()[0]
    bottom = top.splitVertically()
    try:
        top.setSplitFraction(0.6)          # top row 60% of height
    except Exception:
        pass
    # Split the top row horizontally → Scene Viewer (left) | Layout (right).
    layout_pane = top.splitHorizontally()
    try:
        top.setSplitFraction(0.58)         # viewer 58% of the top row width
    except Exception:
        pass

    _set_python_panel(layout_pane, LAYOUT_INTERFACE)
    _set_python_panel(bottom, ASSETS_INTERFACE)

    # Pin the Scene Viewer (left/top pane) to the layout camera + lock the look.
    sv_tab = next((t for t in top.tabs()
                   if t.type() == hou.paneTabType.SceneViewer), None)
    cam = _layout_cam(inst)
    if sv_tab is not None and cam is not None:
        try:
            sv_tab.curViewport().setCamera(cam)
        except Exception:
            pass
    # Preferred viewer look: grids off, dome work light.
    _apply_viewport_look(sv_tab)

    panel.setName("Asset Forge")
    return panel
