import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinterdnd2 import DND_FILES, TkinterDnD

MIB = 1024 * 1024
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
BASE = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))


def find_tool(name):
    # prefer the copy bundled next to the script / inside the exe, else whatever is on PATH
    bundled = os.path.join(BASE, name + (".exe" if sys.platform == "win32" else ""))
    return bundled if os.path.isfile(bundled) else shutil.which(name) or bundled


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")


# ---------- ffmpeg helpers (no UI, so they can be tested on their own) ----------

def probe(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                         capture_output=True, check=True, creationflags=NO_WINDOW).stdout
    info = json.loads(out)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise ValueError("no video stream in that file")
    num, den = (float(x) for x in video.get("avg_frame_rate", "0/0").split("/"))
    return {
        "duration": float(info["format"]["duration"]),
        "fps": num / den if num and den else 30.0,
        "bitrate": int(info["format"].get("bit_rate", 0)),
        "size": int(info["format"].get("size", 0)),
        "has_audio": any(s["codec_type"] == "audio" for s in info["streams"]),
    }


def grab_frame(path, t, width=260):
    """Return one frame at time t as base64 PNG (what tk.PhotoImage wants), or None."""
    png = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                          "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                         capture_output=True, creationflags=NO_WINDOW).stdout
    return base64.b64encode(png) if png else None


def run_ffmpeg(args, duration, on_progress=lambda f: None):
    p = subprocess.Popen([FFMPEG, "-y", "-v", "error", "-nostats", "-progress", "pipe:1", *args],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=NO_WINDOW)
    # stderr shares the stdout pipe: a separate, unread stderr pipe can fill up and freeze ffmpeg
    errors = []
    for line in p.stdout:
        key, sep, val = line.strip().partition("=")
        if key == "out_time_us" and val.isdigit():
            on_progress(min(int(val) / 1e6 / duration, 1.0))
        elif not sep or " " in key:  # progress lines are bare key=value, anything else is a log message
            errors.append(line.strip())
    if p.wait():
        raise RuntimeError("\n".join(errors)[-600:] or "ffmpeg failed")


def output_path(src, ext):
    base = os.path.splitext(src)[0] + "_mazvid"
    path, n = base + ext, 1
    while os.path.exists(path):
        path, n = f"{base}_{n}{ext}", n + 1
    return path


def cut(src, start, end):
    # -ss before -i is fast, and frame-accurate because we always re-encode
    return ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", src]


def to_mp3(src, start, end, on_progress=lambda f: None):
    out = output_path(src, ".mp3")
    run_ffmpeg([*cut(src, start, end), "-vn", "-c:a", "libmp3lame", "-q:a", "2", out], end - start, on_progress)
    return out


def trim_only(src, start, end, mute, on_progress=lambda f: None):
    out = output_path(src, ".mp4")
    audio = ["-an"] if mute else ["-c:a", "aac", "-b:a", "192k"]
    run_ffmpeg([*cut(src, start, end), "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
                *audio, "-movflags", "+faststart", out], end - start, on_progress)
    return out


def plan_bitrates(target_mb, duration, mute, src_bitrate):
    """Bits/sec for video and audio so the file lands ~1 MiB under target."""
    total = (target_mb - 1) * MIB * 8 * 0.98 / duration  # 2% headroom for mp4 container overhead
    audio = 0 if mute else int(min(128_000, max(32_000, total * 0.1)))
    video = int(total - audio)
    if src_bitrate:
        video = min(video, src_bitrate)  # never inflate past the source's quality
    return video, audio


def max_height(video_bps):
    # low bitrates look much better at a lower resolution than as blocky 1080p
    for bps, h in ((2_500_000, 1080), (1_200_000, 720), (500_000, 480)):
        if video_bps >= bps:
            return h
    return 360


def compress(src, start, end, target_mb, mute, src_bitrate, on_progress=lambda f: None):
    duration = end - start
    if target_mb <= 1:
        raise ValueError("target size has to be more than 1 MB")
    video, audio = plan_bitrates(target_mb, duration, mute, src_bitrate)
    if video < 40_000:
        raise ValueError(f"{duration:.0f}s is too long to fit in {target_mb:g} MB, trim it shorter")
    if src_bitrate and src_bitrate * duration / 8 <= (target_mb - 1) * MIB:
        # already fits: forcing x264 to a bitrate the video can't use makes 2-pass fail to converge
        out = trim_only(src, start, end, mute, on_progress)
        if os.path.getsize(out) <= target_mb * MIB:
            return out
        os.remove(out)
    out = output_path(src, ".mp4")
    log = os.path.join(tempfile.gettempdir(), "mazvid_2pass")
    audio_args = ["-an"] if mute else ["-c:a", "aac", "-b:a", str(audio)]
    for _ in range(3):
        common = [*cut(src, start, end), "-vf", f"scale=-2:'min(ih,{max_height(video)})'",
                  "-c:v", "libx264", "-preset", "medium", "-b:v", str(video), "-pix_fmt", "yuv420p",
                  "-passlogfile", log,
                  # the null and mp4 outputs default to different frame timing, so on variable-framerate
                  # clips pass 2 would get duplicated frames pass 1 never saw ("Incomplete MB-tree stats file")
                  "-fps_mode", "passthrough"]
        run_ffmpeg([*common, "-pass", "1", "-an", "-f", "null", os.devnull], duration, lambda f: on_progress(f / 2))
        run_ffmpeg([*common, "-pass", "2", *audio_args, "-movflags", "+faststart", out], duration,
                   lambda f: on_progress(0.5 + f / 2))
        size = os.path.getsize(out)
        if size <= target_mb * MIB:
            return out
        # overshot: shrink the video bitrate by how much we missed, plus a bit, and redo
        video = int(video * ((target_mb - 1) * MIB / size) * 0.95)
    os.remove(out)
    raise RuntimeError(f"couldn't get it under {target_mb:g} MB")


def fmt_time(t):
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:06.3f}"


# ---------- UI ----------

BG, PANEL, FG, MUTED, ACCENT = "#1e1f22", "#2b2d31", "#e3e5e8", "#949ba4", "#5865f2"


class App:
    def __init__(self, root):
        self.root = root
        self.path = None
        self.info = None
        self.photos = {}
        self.preview_jobs = {}
        self.progress = 0.0
        self.status = ""
        self.busy = False

        root.title("maz.vid")
        root.configure(bg=BG)
        root.resizable(False, False)
        self.style_widgets()

        main = ttk.Frame(root, padding=16)
        main.pack(fill="both", expand=True)

        # drop zone
        self.drop = tk.Label(main, text="drop a video here  ·  or click to browse", bg=PANEL, fg=MUTED,
                             font=("Segoe UI", 11), height=3, cursor="hand2")
        self.drop.pack(fill="x")
        self.drop.drop_target_register(DND_FILES)
        self.drop.dnd_bind("<<Drop>>", lambda e: self.load(root.tk.splitlist(e.data)[0]))
        self.drop.bind("<Button-1>", lambda e: self.browse())

        # trim: start and end, each with a frame preview
        trim = ttk.Frame(main)
        trim.pack(fill="x", pady=(14, 0))
        self.start = tk.DoubleVar(value=0)
        self.end = tk.DoubleVar(value=0)
        self.scales, self.previews, self.time_labels = {}, {}, {}
        for col, (name, var) in enumerate((("start", self.start), ("end", self.end))):
            box = ttk.Frame(trim, style="Panel.TFrame", padding=10)
            box.grid(row=0, column=col, padx=(0, 10) if col == 0 else 0, sticky="n")
            ttk.Label(box, text=f"trim {name}", style="Panel.TLabel").pack(anchor="w")
            self.previews[name] = tk.Label(box, bg="#111214", width=260, height=146, image=self.blank())
            self.previews[name].pack(pady=6)
            self.scales[name] = ttk.Scale(box, from_=0, to=1, variable=var, length=260,
                                          command=lambda _, n=name: self.on_trim(n))
            self.scales[name].pack()
            row = ttk.Frame(box, style="Panel.TFrame")
            row.pack(fill="x", pady=(4, 0))
            ttk.Button(row, text="◀", width=3, command=lambda n=name: self.step(n, -1)).pack(side="left")
            self.time_labels[name] = ttk.Label(row, text="--:--", style="Panel.TLabel", anchor="center")
            self.time_labels[name].pack(side="left", expand=True, fill="x")
            ttk.Button(row, text="▶", width=3, command=lambda n=name: self.step(n, 1)).pack(side="right")

        self.length_label = ttk.Label(main, text="", style="Muted.TLabel")
        self.length_label.pack(anchor="w", pady=(8, 0))

        # options
        opts = ttk.Frame(main)
        opts.pack(fill="x", pady=(10, 0))
        self.mode = tk.StringVar(value="compress")
        ttk.Label(opts, text="mode").grid(row=0, column=0, sticky="w", padx=(0, 12))
        for i, (text, val) in enumerate((("compress", "compress"), ("trim only", "trim"), ("mp3", "mp3"))):
            ttk.Radiobutton(opts, text=text, value=val, variable=self.mode,
                            command=self.update_options).grid(row=0, column=i + 1, sticky="w", padx=(0, 12))

        self.size = tk.StringVar(value="8")
        ttk.Label(opts, text="size").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.size_widgets = [
            ttk.Radiobutton(opts, text="8 MB", value="8", variable=self.size),
            ttk.Radiobutton(opts, text="50 MB", value="50", variable=self.size),
            ttk.Radiobutton(opts, text="custom", value="custom", variable=self.size),
        ]
        for i, w in enumerate(self.size_widgets):
            w.grid(row=1, column=i + 1, sticky="w", pady=(8, 0))
        self.custom = ttk.Entry(opts, width=6)
        self.custom.grid(row=1, column=4, sticky="w", pady=(8, 0))
        self.custom.bind("<FocusIn>", lambda e: self.size.set("custom"))
        self.size_widgets.append(self.custom)
        ttk.Label(opts, text="MB", style="Muted.TLabel").grid(row=1, column=5, sticky="w", padx=(4, 0), pady=(8, 0))

        self.mute = tk.BooleanVar()
        self.mute_box = ttk.Checkbutton(opts, text="remove audio", variable=self.mute)
        self.mute_box.grid(row=2, column=1, columnspan=2, sticky="w", pady=(8, 0))

        # go + progress
        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(16, 0))
        self.go_button = ttk.Button(bottom, text="go", style="Accent.TButton", command=self.go)
        self.go_button.pack(side="left")
        self.bar = ttk.Progressbar(bottom, maximum=1.0, length=300)
        self.bar.pack(side="left", padx=(12, 0), fill="x", expand=True)
        self.status_label = ttk.Label(main, text="", style="Muted.TLabel", wraplength=560)
        self.status_label.pack(anchor="w", pady=(8, 0))

        self.poll()

    def style_widgets(self):
        s = ttk.Style()
        s.theme_use("clam")
        font = ("Segoe UI", 10)
        s.configure(".", background=BG, foreground=FG, font=font, bordercolor=PANEL, focuscolor=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("Panel.TLabel", background=PANEL, foreground=FG)
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("TButton", background=PANEL, foreground=FG, borderwidth=0, padding=(8, 4))
        s.map("TButton", background=[("active", "#3a3c42")])
        s.configure("Accent.TButton", background=ACCENT, foreground="white", padding=(22, 6),
                    font=("Segoe UI", 10, "bold"))
        s.map("Accent.TButton", background=[("disabled", "#3a3c42"), ("active", "#4752c4")])
        for w in ("TRadiobutton", "TCheckbutton"):
            s.configure(w, background=BG, foreground=FG, indicatorbackground=PANEL, indicatorforeground=FG)
            s.map(w, background=[("active", BG)], foreground=[("disabled", "#5c5f66")],
                  indicatorbackground=[("selected", ACCENT)])
        s.configure("TEntry", fieldbackground=PANEL, foreground=FG, insertcolor=FG, borderwidth=0)
        s.map("TEntry", fieldbackground=[("disabled", BG)])
        s.configure("Horizontal.TScale", background=ACCENT, troughcolor="#111214", borderwidth=0)
        s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL, borderwidth=0)

    def blank(self):
        self.photos.setdefault("blank", tk.PhotoImage(width=260, height=146))
        return self.photos["blank"]

    # --- loading ---

    def browse(self):
        path = filedialog.askopenfilename(filetypes=[("video", "*.mp4 *.mov *.mkv *.webm *.avi"), ("all", "*.*")])
        if path:
            self.load(path)

    def load(self, path):
        if self.busy:
            return
        try:
            self.info = probe(path)
        except Exception as e:
            messagebox.showerror("maz.vid", f"couldn't read that file:\n{e}")
            return
        self.path = path
        dur = self.info["duration"]
        self.drop.config(fg=FG, text=f"{os.path.basename(path)}\n{fmt_time(dur)}  ·  {self.info['size'] / MIB:.1f} MB")
        for name in ("start", "end"):
            self.scales[name].config(to=dur)
        self.start.set(0)
        self.end.set(dur)
        self.on_trim("start")
        self.on_trim("end")
        self.status = ""

    # --- trimming ---

    def on_trim(self, name):
        if not self.info:
            return
        frame = 1 / self.info["fps"]
        # keep start before end
        if name == "start" and self.start.get() > self.end.get() - frame:
            self.start.set(max(0, self.end.get() - frame))
        if name == "end" and self.end.get() < self.start.get() + frame:
            self.end.set(min(self.info["duration"], self.start.get() + frame))
        t = (self.start if name == "start" else self.end).get()
        self.time_labels[name].config(text=f"{fmt_time(t)}  ·  frame {round(t * self.info['fps'])}")
        self.length_label.config(text=f"selection: {fmt_time(self.end.get() - self.start.get())}")
        # debounce: only grab a frame once the slider stops moving
        if name in self.preview_jobs:
            self.root.after_cancel(self.preview_jobs[name])
        self.preview_jobs[name] = self.root.after(120, lambda: self.show_frame(name))

    def step(self, name, direction):
        if not self.info:
            return
        var = self.start if name == "start" else self.end
        var.set(min(max(var.get() + direction / self.info["fps"], 0), self.info["duration"]))
        self.on_trim(name)

    def show_frame(self, name):
        t = (self.start if name == "start" else self.end).get()
        if name == "end":
            t -= 1 / self.info["fps"]  # the end point is exclusive, show the last frame kept
        data = grab_frame(self.path, max(0, min(t, self.info["duration"] - 0.05)))
        if data:
            self.photos[name] = tk.PhotoImage(data=data)
            self.previews[name].config(image=self.photos[name])

    def update_options(self):
        mode = self.mode.get()
        for w in self.size_widgets:
            w.state(["!disabled"] if mode == "compress" else ["disabled"])
        self.mute_box.state(["disabled"] if mode == "mp3" else ["!disabled"])

    # --- running ---

    def go(self):
        if not self.path:
            messagebox.showwarning("maz.vid", "drop a video in first")
            return
        mode, start, end, mute = self.mode.get(), self.start.get(), self.end.get(), self.mute.get()
        if mode == "mp3" and not self.info["has_audio"]:
            messagebox.showwarning("maz.vid", "that video has no audio")
            return
        if mode == "compress":
            try:
                target = float(self.custom.get() if self.size.get() == "custom" else self.size.get())
            except ValueError:
                messagebox.showwarning("maz.vid", "custom size has to be a number")
                return
            job = lambda p: compress(self.path, start, end, target, mute, self.info["bitrate"], p)
        elif mode == "trim":
            job = lambda p: trim_only(self.path, start, end, mute, p)
        else:
            job = lambda p: to_mp3(self.path, start, end, p)
        self.busy = True
        self.go_button.state(["disabled"])
        self.progress, self.status = 0.0, "working..."
        threading.Thread(target=self.worker, args=(job,), daemon=True).start()

    def worker(self, job):
        # runs off the UI thread, so it only sets attributes that poll() picks up
        def on_progress(f):
            self.progress = f
        try:
            out = job(on_progress)
            self.progress = 1.0
            self.status = f"saved {os.path.basename(out)}  ({os.path.getsize(out) / MIB:.2f} MB)"
        except Exception as e:
            self.status = f"failed: {e}"
        self.busy = False

    def poll(self):
        self.bar["value"] = self.progress
        self.status_label.config(text=self.status)
        if not self.busy:
            self.go_button.state(["!disabled"])
        self.root.after(100, self.poll)


if __name__ == "__main__":
    App(TkinterDnD.Tk())
    tk.mainloop()
