#!/usr/bin/env python3
"""
DVD Remux GUI
=============
Losslessly remux DVD VIDEO_TS folders into MKV/MP4 (no re-encode).
Video stays MPEG-2, audio stays AC-3 -- only the container changes, so
quality is identical and conversion is fast.

Requirements:
  * Python 3.8+ (Tkinter ships with the standard Windows/macOS installers)
  * ffmpeg AND ffprobe on PATH (or point to them in the app)
        Windows : winget install Gyan.FFmpeg
        macOS   : brew install ffmpeg
        Linux   : sudo apt install ffmpeg

Run:
    python dvd_remux_gui.py
"""

import os
import re
import shutil
import subprocess
import threading
import queue
import tempfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND_OK = True
    _BASE = TkinterDnD.Tk
except Exception:
    _DND_OK = False
    _BASE = tk.Tk

VOB_RE = re.compile(r"^VTS_(\d+)_([1-9])\.VOB$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
#  Core scanning / remux logic (no GUI here)
# --------------------------------------------------------------------------- #
def find_discs(roots, report=None):
    """
    Yield disc descriptors found under the given roots:
        {"vob_dir": folder that holds the VOB files,
         "name":    disc name used for the output file,
         "root":    folder to feed the dvdvideo demuxer,
         "structured": True if a real VIDEO_TS subfolder exists}
    Handles both DVD layouts:
      * structured -> <disc>/VIDEO_TS/VTS_*.VOB
      * flattened  -> <disc>/VTS_*.VOB     (DVD files loose in the folder)
    """
    seen = set()

    def _err(e):
        if report:
            report(f"  walk error: {e}")

    def _emit(vob_dir, name, droot, structured):
        rp = os.path.realpath(vob_dir)
        if rp in seen:
            return None
        seen.add(rp)
        return {"vob_dir": vob_dir, "name": name or "DVD",
                "root": droot, "structured": structured}

    for root in roots:
        root = root.rstrip("/\\") or root
        if report:
            report(f"  scanning: {root}")
        if not os.path.isdir(root):
            if report:
                report(f"  NOT a reachable folder: {root}")
            continue
        hit = False
        for dirpath, dirnames, files in os.walk(root, onerror=_err):
            base = os.path.basename(dirpath).upper()
            upper = {fn.upper() for fn in files}
            if base == "VIDEO_TS":
                disc = os.path.dirname(dirpath)
                d = _emit(dirpath,
                          os.path.basename(disc) or os.path.basename(dirpath),
                          disc, True)
                if d:
                    hit = True
                    yield d
            elif "VIDEO_TS.IFO" in upper or any(VOB_RE.match(fn) for fn in files):
                d = _emit(dirpath, os.path.basename(dirpath), dirpath, False)
                if d:
                    hit = True
                    yield d
        if not hit and report:
            report(f"  no DVD video found under: {root} -- contents:")
            try:
                entries = sorted(os.listdir(root))
            except OSError as e:
                report(f"    (cannot list: {e})")
                entries = []
            for name in entries[:25]:
                tag = "DIR " if os.path.isdir(os.path.join(root, name)) else "file"
                report(f"    {tag} {name}")
            if len(entries) > 25:
                report(f"    ... (+{len(entries) - 25} more)")


def group_titles(video_ts):
    """
    Group the title VOBs in a VIDEO_TS folder by VTS set number.
    Returns a dict: {set_number: [ordered VOB full paths]} ignoring menu (_0) VOBs.
    Each VTS set is treated as one DVD 'title'.
    """
    sets = {}
    try:
        names = os.listdir(video_ts)
    except OSError:
        return sets
    for name in names:
        m = VOB_RE.match(name)
        if m:
            setno = int(m.group(1))
            sets.setdefault(setno, []).append(name)
    # order parts within each set and build full paths
    ordered = {}
    for setno, files in sets.items():
        files.sort(key=lambda n: int(VOB_RE.match(n).group(2)))
        ordered[setno] = [os.path.join(video_ts, f) for f in files]
    return ordered


def concat_arg(vob_paths):
    return "concat:" + "|".join(vob_paths)


def probe_duration(ffprobe, vob_paths):
    """Return duration in seconds (float) of a concatenated VOB set, or 0.0."""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", "-i", concat_arg(vob_paths)],
            capture_output=True, text=True, timeout=120,
        )
        return float(out.stdout.strip())
    except Exception:
        # fall back to size-based pseudo-duration (just for ranking/filtering)
        return 0.0


def total_size(vob_paths):
    return sum(os.path.getsize(p) for p in vob_paths if os.path.exists(p))


def probe_dvd_title(ffprobe, disc_root, title):
    """
    Probe a logical DVD title via the dvdvideo demuxer.
    Returns duration in seconds, or None if the title doesn't exist.
    """
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-f", "dvdvideo", "-title", str(title),
             "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", "-i", disc_root],
            capture_output=True, text=True, timeout=180,
        )
        if out.returncode != 0:
            return None
        return float(out.stdout.strip())
    except Exception:
        return None


def enumerate_dvd_titles(ffprobe, disc_root, max_titles=99):
    """Return [(title_number, duration_seconds), ...] for a DVD disc root."""
    titles = []
    misses = 0
    for t in range(1, max_titles + 1):
        dur = probe_dvd_title(ffprobe, disc_root, t)
        if dur is None:
            misses += 1
            if misses >= 2:        # two consecutive misses -> stop
                break
            continue
        misses = 0
        titles.append((t, dur))
    return titles


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
class App(_BASE):
    def __init__(self):
        super().__init__()
        self.title("DVD Remux  -  VIDEO_TS to MKV/MP4 (lossless)")
        self.geometry("760x640")
        self.minsize(680, 560)

        self.worker = None
        self.stop_flag = threading.Event()
        self.log_q = queue.Queue()

        self._build_ui()
        if not _DND_OK:
            self._log("Tip: run 'pip install tkinterdnd2' to drag folders onto the list.")
        self.after(100, self._drain_log)

    # ----- layout ---------------------------------------------------------- #
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # Source directories
        src = ttk.LabelFrame(self, text="1. Source folders - add or drag && drop here (searched for VIDEO_TS)")
        src.pack(fill="x", **pad)
        self.src_list = tk.Listbox(src, height=4, selectmode=tk.EXTENDED)
        self.src_list.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        if _DND_OK:
            self.src_list.drop_target_register(DND_FILES)
            self.src_list.dnd_bind("<<Drop>>", self._on_drop)
        btns = ttk.Frame(src)
        btns.pack(side="right", fill="y", padx=6, pady=6)
        ttk.Button(btns, text="Add folder...", command=self.add_source).pack(fill="x")
        ttk.Button(btns, text="Add multiple...", command=self.add_multiple).pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Remove", command=self.remove_source).pack(fill="x", pady=4)
        ttk.Button(btns, text="Scan", command=self.scan_sources).pack(fill="x")

        # Destination
        dst = ttk.LabelFrame(self, text="2. Destination")
        dst.pack(fill="x", **pad)
        self.dest_var = tk.StringVar(
            value=r"\\diskstation\video\family\family\zada sampled\videos\02-mpeg2")
        ttk.Entry(dst, textvariable=self.dest_var).pack(
            side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(dst, text="Browse...", command=self.pick_dest).pack(
            side="right", padx=6, pady=6)

        # Options
        opt = ttk.LabelFrame(self, text="3. Remux options")
        opt.pack(fill="x", **pad)

        # title handling
        ttk.Label(opt, text="Titles:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.title_mode = tk.StringVar(value="each")
        ttk.Radiobutton(opt, text="Each title -> own file", value="each",
                        variable=self.title_mode, command=self._toggle_min).grid(
            row=0, column=1, sticky="w")
        ttk.Radiobutton(opt, text="Main title only", value="main",
                        variable=self.title_mode, command=self._toggle_min).grid(
            row=0, column=2, sticky="w")
        ttk.Radiobutton(opt, text="Filter by min length", value="filter",
                        variable=self.title_mode, command=self._toggle_min).grid(
            row=0, column=3, sticky="w")

        self.min_min = tk.DoubleVar(value=5.0)
        self.min_lbl = ttk.Label(opt, text="Min minutes:")
        self.min_spin = ttk.Spinbox(opt, from_=0, to=600, increment=1,
                                    textvariable=self.min_min, width=6)
        self.min_lbl.grid(row=1, column=2, sticky="e", padx=6)
        self.min_spin.grid(row=1, column=3, sticky="w")
        self._toggle_min()

        # container
        ttk.Label(opt, text="Container:").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.container = tk.StringVar(value="mkv")
        self.rb_mkv = ttk.Radiobutton(opt, text="MKV (recommended)", value="mkv",
                                      variable=self.container)
        self.rb_mkv.grid(row=2, column=1, sticky="w")
        self.rb_mp4 = ttk.Radiobutton(opt, text="MP4", value="mp4",
                                      variable=self.container)
        self.rb_mp4.grid(row=2, column=2, sticky="w")

        # existing-file handling
        ttk.Label(opt, text="If exists:").grid(row=3, column=0, sticky="w", padx=6, pady=4)
        self.on_exist = tk.StringVar(value="skip")
        ttk.Radiobutton(opt, text="Skip", value="skip",
                        variable=self.on_exist).grid(row=3, column=1, sticky="w")
        ttk.Radiobutton(opt, text="Overwrite", value="overwrite",
                        variable=self.on_exist).grid(row=3, column=2, sticky="w")

        # preserve subfolder structure
        self.flat = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Name output after the disc's parent folder",
                        variable=self.flat).grid(row=4, column=1, columnspan=3, sticky="w")

        # DVD demuxer (keep chapters)
        self.use_dvd = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt,
            text="Use DVD demuxer (keep chapters; reads logical titles - MKV only)",
            variable=self.use_dvd, command=self._toggle_dvd).grid(
            row=6, column=0, columnspan=4, sticky="w", padx=6)

        # ffmpeg paths
        tools = ttk.Frame(opt)
        tools.grid(row=5, column=0, columnspan=4, sticky="we", padx=6, pady=6)
        ttk.Label(tools, text="ffmpeg:").pack(side="left")
        self.ffmpeg_var = tk.StringVar(value=shutil.which("ffmpeg") or "ffmpeg")
        ttk.Entry(tools, textvariable=self.ffmpeg_var, width=24).pack(side="left", padx=4)
        ttk.Label(tools, text="ffprobe:").pack(side="left")
        self.ffprobe_var = tk.StringVar(value=shutil.which("ffprobe") or "ffprobe")
        ttk.Entry(tools, textvariable=self.ffprobe_var, width=24).pack(side="left", padx=4)

        # Action buttons
        act = ttk.Frame(self)
        act.pack(fill="x", **pad)
        self.run_btn = ttk.Button(act, text="Start remux", command=self.start)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(act, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.verify_btn = ttk.Button(act, text="Verify", command=self.verify)
        self.verify_btn.pack(side="left", padx=6)
        self.prog = ttk.Progressbar(act, mode="determinate")
        self.prog.pack(side="left", fill="x", expand=True, padx=6)

        # Log
        logf = ttk.LabelFrame(self, text="Log")
        logf.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logf, height=12, wrap="word", state="disabled")
        self.log.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb.set)

        self._toggle_dvd()   # apply initial DVD-mode state (MP4 disabled by default)

    def _toggle_min(self):
        state = "normal" if self.title_mode.get() == "filter" else "disabled"
        self.min_spin.config(state=state)

    def _toggle_dvd(self):
        # The dvdvideo demuxer path preserves chapters and is MKV-only here.
        if self.use_dvd.get():
            self.container.set("mkv")
            self.rb_mp4.config(state="disabled")
        else:
            self.rb_mp4.config(state="normal")

    # ----- source/dest helpers -------------------------------------------- #
    def add_source(self):
        d = filedialog.askdirectory(title="Add a source folder")
        if d:
            self.src_list.insert(tk.END, d)

    def add_multiple(self):
        """Pick a parent folder, then multi-select which subfolders to add."""
        parent = filedialog.askdirectory(
            title="Choose a parent folder (you'll pick subfolders next)")
        if not parent:
            return
        try:
            subs = sorted(d for d in os.listdir(parent)
                          if os.path.isdir(os.path.join(parent, d)))
        except OSError as e:
            messagebox.showerror("Add multiple", str(e))
            return
        if not subs:
            # No subfolders: just add the parent itself (scanned recursively).
            self.src_list.insert(tk.END, parent)
            return

        win = tk.Toplevel(self)
        win.title("Select folders to add")
        win.geometry("520x380")
        win.transient(self)
        ttk.Label(win,
                  text=f"Subfolders of: {parent}   (Ctrl/Shift-click to select multiple)").pack(
            anchor="w", padx=8, pady=6)
        lb = tk.Listbox(win, selectmode=tk.EXTENDED)
        for d in subs:
            lb.insert(tk.END, d)
        lb.pack(fill="both", expand=True, padx=8, pady=4)
        lb.select_set(0, tk.END)  # default: everything selected

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="Select all",
                   command=lambda: lb.select_set(0, tk.END)).pack(side="left")
        ttk.Button(bar, text="Clear",
                   command=lambda: lb.select_clear(0, tk.END)).pack(side="left", padx=6)

        def do_add():
            existing = set(self.src_list.get(0, tk.END))
            for i in lb.curselection():
                full = os.path.join(parent, subs[i])
                if full not in existing:
                    self.src_list.insert(tk.END, full)
            win.destroy()

        ttk.Button(bar, text="Add selected", command=do_add).pack(side="right")
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right", padx=6)
        win.grab_set()

    def _on_drop(self, event):
        """Handle folders dragged from the file manager onto the list."""
        added = skipped = 0
        existing = set(self.src_list.get(0, tk.END))
        for p in self.tk.splitlist(event.data):
            p = p.strip()
            if os.path.isdir(p):
                if p not in existing:
                    self.src_list.insert(tk.END, p)
                    existing.add(p)
                    added += 1
            else:
                skipped += 1
        msg = f"Drag-drop: added {added} folder(s)"
        if skipped:
            msg += f", ignored {skipped} non-folder item(s)"
        self._log(msg)

    def remove_source(self):
        for i in reversed(self.src_list.curselection()):
            self.src_list.delete(i)

    def pick_dest(self):
        d = filedialog.askdirectory(title="Choose destination")
        if d:
            self.dest_var.set(d)

    def scan_sources(self):
        roots = list(self.src_list.get(0, tk.END))
        if not roots:
            messagebox.showinfo("Scan", "Add at least one source folder first.")
            return
        found = list(find_discs(roots, report=self._log))
        self._log(f"Scan: found {len(found)} disc(s).")
        use_dvd = self.use_dvd.get()
        ffprobe = self.ffprobe_var.get().strip()
        threading.Thread(
            target=self._scan_worker, args=(found, use_dvd, ffprobe), daemon=True
        ).start()

    def _scan_worker(self, found, use_dvd, ffprobe):
        for disc in found:
            name = disc["name"]
            layout = "structured" if disc["structured"] else "flattened"
            if use_dvd and disc["structured"]:
                titles = enumerate_dvd_titles(ffprobe, disc["root"])
                desc = ", ".join(f"#{t}:{d/60:.0f}m" for t, d in titles) or "none"
                self._log(f"  {name} [{layout}]  ->  {len(titles)} DVD title(s)  [{desc}]")
            else:
                sets = group_titles(disc["vob_dir"])
                note = "  (flattened: concat only)" if (use_dvd and not disc["structured"]) else ""
                self._log(f"  {name} [{layout}]  ->  {len(sets)} VTS set(s){note}")

    # ----- logging -------------------------------------------------------- #
    def _log(self, msg):
        self.log_q.put(msg)

    def _drain_log(self):
        while not self.log_q.empty():
            msg = self.log_q.get_nowait()
            self.log.config(state="normal")
            self.log.insert(tk.END, msg + "\n")
            self.log.see(tk.END)
            self.log.config(state="disabled")
        self.after(100, self._drain_log)

    # ----- run ------------------------------------------------------------ #
    def start(self):
        roots = list(self.src_list.get(0, tk.END))
        dest = self.dest_var.get().strip()
        if not roots:
            messagebox.showinfo("Start", "Add at least one source folder.")
            return
        if not dest:
            messagebox.showinfo("Start", "Choose a destination.")
            return
        ffmpeg = self.ffmpeg_var.get().strip()
        if not (shutil.which(ffmpeg) or os.path.exists(ffmpeg)):
            messagebox.showerror("ffmpeg", "ffmpeg not found. Install it or set the path.")
            return

        self.stop_flag.clear()
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        cfg = dict(
            roots=roots, dest=dest,
            mode=self.title_mode.get(),
            min_sec=self.min_min.get() * 60.0,
            ext="mkv" if self.use_dvd.get() else self.container.get(),
            on_exist=self.on_exist.get(),
            use_dvd=self.use_dvd.get(),
            ffmpeg=ffmpeg, ffprobe=self.ffprobe_var.get().strip(),
        )
        self.worker = threading.Thread(target=self._run_job, args=(cfg,), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_flag.set()
        self._log("Stop requested -- finishing current file...")

    def _build_jobs(self, cfg, discs):
        """
        Returns a list of job dicts. Two shapes:
          DVD demuxer : {"kind":"dvd", "root":disc_root, "title":N, "dur":sec, "out":path}
          VOB concat  : {"kind":"concat", "vobs":[...], "dur":sec, "out":path}
        Flattened discs (no VIDEO_TS subfolder) can't use the dvdvideo demuxer,
        so they fall back to concat automatically.
        """
        jobs = []
        for disc in discs:
            disc_name = disc["name"]
            vt = disc["vob_dir"]
            disc_root = disc["root"]
            use_dvd = cfg["use_dvd"] and disc["structured"]
            if cfg["use_dvd"] and not disc["structured"]:
                self._log(f"  {disc_name}: flattened disc -> concat "
                          "(chapters not preserved)")

            if use_dvd:
                titles = enumerate_dvd_titles(cfg["ffprobe"], disc_root)
                if not titles:
                    self._log(f"  {disc_name}: dvdvideo demuxer found no titles, skipping")
                    continue
                ranked = sorted(titles, key=lambda t: t[1], reverse=True)  # by duration
                if cfg["mode"] == "main":
                    chosen = [ranked[0]]
                elif cfg["mode"] == "filter":
                    chosen = [t for t in ranked if t[1] >= cfg["min_sec"]] or [ranked[0]]
                else:
                    chosen = ranked
                chosen = sorted(chosen, key=lambda t: t[0])  # back to title order
                multi = len(chosen) > 1
                for idx, (tno, dur) in enumerate(chosen, start=1):
                    name = (f"{disc_name}_title{idx:02d}.mkv" if multi
                            else f"{disc_name}.mkv")
                    jobs.append({"kind": "dvd", "root": disc_root, "title": tno,
                                 "dur": dur, "out": os.path.join(cfg["dest"], name)})
            else:
                sets = group_titles(vt)
                if not sets:
                    self._log(f"  {disc_name}: no title VOBs, skipping")
                    continue
                ranked = []
                for setno, vobs in sets.items():
                    dur = probe_duration(cfg["ffprobe"], vobs)
                    ranked.append((setno, vobs, dur, total_size(vobs)))
                ranked.sort(key=lambda t: (t[2], t[3]), reverse=True)
                if cfg["mode"] == "main":
                    chosen = [ranked[0]]
                elif cfg["mode"] == "filter":
                    chosen = [r for r in ranked if r[2] >= cfg["min_sec"]] or [ranked[0]]
                else:
                    chosen = ranked
                chosen = sorted(chosen, key=lambda r: r[0])  # title/set order
                multi = len(chosen) > 1
                for idx, (setno, vobs, dur, _sz) in enumerate(chosen, start=1):
                    name = (f"{disc_name}_title{idx:02d}.{cfg['ext']}" if multi
                            else f"{disc_name}.{cfg['ext']}")
                    jobs.append({"kind": "concat", "vobs": vobs, "dur": dur,
                                 "out": os.path.join(cfg["dest"], name)})
        return jobs

    @staticmethod
    def _build_cmd(cfg, job):
        ff = cfg["ffmpeg"]
        out = job["out"]
        if job["kind"] == "dvd":
            # dvdvideo demuxer: preserves chapters; map A/V/subs only (no data/nav)
            return [ff, "-hide_banner", "-loglevel", "warning", "-y",
                    "-progress", "pipe:1", "-nostats",
                    "-f", "dvdvideo", "-title", str(job["title"]),
                    "-i", job["root"],
                    "-map", "0:v?", "-map", "0:a?", "-map", "0:s?",
                    "-dn", "-c", "copy", out]
        # concat mode: exclude DVD data/nav streams that Matroska/MP4 reject
        if cfg["ext"] == "mp4":
            return [ff, "-hide_banner", "-loglevel", "warning", "-y",
                    "-progress", "pipe:1", "-nostats",
                    "-analyzeduration", "100M", "-probesize", "100M",
                    "-i", concat_arg(job["vobs"]),
                    "-map", "0:v?", "-map", "0:a?",
                    "-dn", "-sn", "-c", "copy", out]
        return [ff, "-hide_banner", "-loglevel", "warning", "-y",
                    "-progress", "pipe:1", "-nostats",
                "-analyzeduration", "100M", "-probesize", "100M",
                "-i", concat_arg(job["vobs"]),
                "-map", "0:v?", "-map", "0:a?", "-map", "0:s?",
                "-dn", "-c", "copy", out]

    def verify(self):
        """Probe each expected output file and report duration/codecs/tracks."""
        roots = list(self.src_list.get(0, tk.END))
        if not roots:
            messagebox.showinfo("Verify", "Add source folder(s) first.")
            return
        cfg = dict(
            roots=roots, dest=self.dest_var.get().strip(),
            mode=self.title_mode.get(), min_sec=self.min_min.get() * 60.0,
            ext="mkv" if self.use_dvd.get() else self.container.get(),
            use_dvd=self.use_dvd.get(),
            ffmpeg=self.ffmpeg_var.get().strip(),
            ffprobe=self.ffprobe_var.get().strip(),
        )
        threading.Thread(target=self._verify_worker, args=(cfg,), daemon=True).start()

    @staticmethod
    def _ffprobe_info(ffprobe, path):
        """Return (duration_s, 'vcodec WxH', n_audio, n_subs) or None."""
        import json
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-print_format", "json",
                 "-show_format", "-show_streams", path],
                capture_output=True, text=True)
            if r.returncode != 0:
                return None
            d = json.loads(r.stdout)
        except Exception:
            return None
        dur = float(d.get("format", {}).get("duration", 0) or 0)
        vcodec, w, h, na, ns = "?", 0, 0, 0, 0
        for st in d.get("streams", []):
            t = st.get("codec_type")
            if t == "video" and vcodec == "?":
                vcodec = st.get("codec_name", "?")
                w, h = st.get("width", 0), st.get("height", 0)
            elif t == "audio":
                na += 1
            elif t == "subtitle":
                ns += 1
        vinfo = f"{vcodec} {w}x{h}" if w else vcodec
        return dur, vinfo, na, ns

    def _verify_worker(self, cfg):
        self.verify_btn.config(state="disabled")
        try:
            discs = list(find_discs(cfg["roots"], report=self._log))
            if not discs:
                self._log("Verify: no DVD video found.")
                return
            self._log("Verify: probing expected outputs ...")
            jobs = self._build_jobs(cfg, discs)
            ok = miss = bad = 0
            for job in jobs:
                out = job["out"]
                base = os.path.basename(out)
                if not os.path.exists(out):
                    self._log(f"  MISSING: {base}")
                    miss += 1
                    continue
                info = self._ffprobe_info(cfg["ffprobe"], out)
                if not info:
                    self._log(f"  UNREADABLE: {base}")
                    bad += 1
                    continue
                dur, vinfo, na, ns = info
                src = job["dur"]
                size_mb = os.path.getsize(out) / 1e6
                warn = ""
                if src and dur and abs(dur - src) / src > 0.02:
                    warn = f"  <-- source is {src/60:.1f} min, check!"
                    bad += 1
                else:
                    ok += 1
                self._log(f"  OK {base}: {dur/60:.1f} min, {vinfo}, "
                          f"{na} audio, {ns} subs, {size_mb:.0f} MB{warn}")
            self._log(f"Verify done: {ok} good, {miss} missing, {bad} flagged.")
        finally:
            self.verify_btn.config(state="normal")

    def _run_ffmpeg(self, cmd, dur, base_value):
        """
        Run ffmpeg, parsing its -progress output to drive the progress bar.
        Overall bar value = base_value + (fraction of this title done).
        Returns (returncode, stderr_text).
        """
        errf = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                    text=True, bufsize=1)
            for line in proc.stdout:
                if self.stop_flag.is_set():
                    proc.terminate()
                    break
                line = line.strip()
                # ffmpeg emits out_time_us (and out_time_ms, which is also us)
                if dur and (line.startswith("out_time_us=") or
                            line.startswith("out_time_ms=")):
                    val = line.split("=", 1)[1]
                    if val.isdigit():
                        frac = min(int(val) / 1_000_000.0 / dur, 1.0)
                        self.prog.config(value=base_value + frac)
            proc.wait()
            errf.seek(0)
            return proc.returncode, errf.read()
        finally:
            errf.close()

    def _run_job(self, cfg):
        try:
            os.makedirs(cfg["dest"], exist_ok=True)
            discs = list(find_discs(cfg["roots"], report=self._log))
            if not discs:
                self._log("No DVD video found.")
                return

            mode_lbl = "DVD demuxer (chapters kept)" if cfg["use_dvd"] else "VOB concat"
            self._log(f"Building work list using: {mode_lbl} ...")
            jobs = self._build_jobs(cfg, discs)

            total = len(jobs)
            self._log(f"Queued {total} title(s) to remux.")
            self.prog.config(maximum=max(total, 1), value=0)

            done = 0
            for job in jobs:
                if self.stop_flag.is_set():
                    self._log("Stopped.")
                    break
                done += 1
                out_path = job["out"]
                base = os.path.basename(out_path)

                if os.path.exists(out_path) and cfg["on_exist"] == "skip":
                    # Don't let a broken/incomplete leftover block the job.
                    # A real remux is large; anything under ~1 MB is junk from a
                    # failed earlier run, so redo it.
                    try:
                        sz = os.path.getsize(out_path)
                    except OSError:
                        sz = 0
                    if sz >= 1_000_000:
                        self._log(f"[{done}/{total}] skip (exists): {base}")
                        self.prog.config(value=done)
                        continue
                    self._log(f"[{done}/{total}] redoing (incomplete {sz} bytes): {base}")

                dur = job["dur"]
                mins = f"{dur/60:.1f} min" if dur else "unknown length"
                if job["kind"] == "dvd":
                    extra = f"DVD title {job['title']}"
                else:
                    extra = f"{len(job['vobs'])} VOB part(s)"
                self._log(f"[{done}/{total}] {base}  ({mins}, {extra})")

                base_value = done - 1
                self.prog.config(value=base_value)
                cmd = self._build_cmd(cfg, job)
                try:
                    rc, err = self._run_ffmpeg(cmd, dur, base_value)
                    if rc == 0:
                        self.prog.config(value=done)
                        for wl in err.strip().splitlines()[:4]:
                            if wl.strip():
                                self._log(f"      ffmpeg: {wl.strip()[:200]}")
                        info = self._ffprobe_info(cfg["ffprobe"], out_path)
                        if info and dur and info[0] and info[0] < dur * 0.97:
                            self._log(f"      WARNING: output {info[0]/60:.1f} min < "
                                      f"source {dur/60:.1f} min -- possible truncation!")
                        self._log(f"      done -> {out_path}")
                    else:
                        self._log(f"      FFMPEG ERROR: {err.strip()[:400]}")
                except Exception as e:
                    self._log(f"      ERROR: {e}")

            self._log("All done." if not self.stop_flag.is_set() else "Halted.")
        finally:
            self.run_btn.config(state="normal")
            self.stop_btn.config(state="disabled")


if __name__ == "__main__":
    App().mainloop()
