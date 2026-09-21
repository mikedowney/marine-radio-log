#!/usr/bin/env python3
"""
Watchkeeper web interface.

A phone-sized view of recent radio traffic: what was heard, how long ago, and
the recording. Serves from the same SQLite database the watchkeeper writes, so
the two run independently -- restarting one does not disturb the other.

    ~/venv_wx/bin/python webui.py
    ~/venv_wx/bin/python webui.py --port 8080

Then open http://<pi-address>:8080 from the phone.

Standard library only. No framework, no build step, no internet: the page has
to load on a boat with no connectivity, so there are no webfonts or CDN links.
"""

import argparse
import html
import json
import logging
import io
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import zipfile

import numpy as np
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watchkeeper as wk
from watchkeeper import TARGET_RMS

log = logging.getLogger("webui")

# Chart-buoy palette: eight hues that stay separable on a dark panel without
# glaring in a night cockpit. Assigned to channels in config order so a channel
# keeps its colour between restarts.
CHANNEL_COLORS = [
    "#E0A458",  # amber
    "#6FA8C7",  # sky
    "#8FBF7F",  # sage
    "#D08C8C",  # rose
    "#A896C4",  # lilac
    "#5FB3A8",  # teal
    "#C9B26E",  # sand
    "#D9776A",  # coral
]


def active_preset(messages, override=None):
    """Work out which preset is being received, so the legend lists the
    channels actually in use rather than every frequency in the config file.

    Chosen by which preset best covers the channel names in recent traffic. An
    explicit --preset wins; with no messages at all we fall back to the first
    preset alphabetically so the legend still shows something useful.
    """
    if override and override in wk.PRESETS:
        return override
    heard = {m["channel"] for m in messages}
    best, best_score = None, -1
    for name, preset in sorted(wk.PRESETS.items()):
        names = {c.name for c in preset["channels"]}
        score = len(heard & names)
        if score > best_score:
            best, best_score = name, score
    return best if best_score > 0 else (override or (sorted(wk.PRESETS)[0]
                                                     if wk.PRESETS else None))


def channel_info(preset_name):
    """Colour and frequency per channel, in config order.

    Ordering by the config rather than by whoever happened to transmit means a
    channel keeps its colour across restarts.
    """
    preset = wk.PRESETS.get(preset_name)
    if not preset:
        return []
    out, seen = [], set()
    for c in preset["channels"]:
        if c.role != "voice" or c.name in seen:
            continue
        seen.add(c.name)
        out.append({
            "name": c.name,
            "color": CHANNEL_COLORS[len(out) % len(CHANNEL_COLORS)],
            "mhz": round(c.freq_hz / 1e6, 3),
        })
    return out


class Data:
    def __init__(self, db_path, audio_dir):
        self.db_path = db_path
        self.audio_dir = os.path.realpath(audio_dir)

    def _conn(self):
        # Read-only, so the watchkeeper's writes are never blocked. WAL lets
        # both processes work at once.
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def messages(self, limit=100):
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, channel, freq_hz, started_at, duration_s, snr_db,"
                " quality, transcript, status FROM messages"
                " ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            try:
                started = datetime.fromisoformat(r["started_at"])
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                epoch = started.timestamp()
            except (ValueError, TypeError):
                epoch = 0.0
            out.append({
                "id": r["id"],
                "channel": r["channel"],
                "mhz": round((r["freq_hz"] or 0) / 1e6, 3),
                "started": epoch,
                "duration": round(r["duration_s"] or 0, 1),
                "snr": round(r["snr_db"], 1) if r["snr_db"] is not None else None,
                # 0-100 readability. Rows recorded before this column existed
                # have none, and are never hidden by the filter.
                "q": (round(r["quality"]) if r["quality"] is not None else None),
                "text": (r["transcript"] or "").strip(),
                "status": r["status"],
            })
        return out

    def message(self, msg_id):
        with self._conn() as c:
            # SELECT * so columns added by a later migration appear without
            # this query needing to be edited in step.
            row = c.execute("SELECT * FROM messages WHERE id=?",
                            (msg_id,)).fetchone()
        return row

    def audio_path(self, msg_id):
        with self._conn() as c:
            row = c.execute("SELECT audio_path FROM messages WHERE id=?",
                            (msg_id,)).fetchone()
        if not row or not row["audio_path"]:
            return None
        path = os.path.realpath(row["audio_path"])
        # Never serve anything outside the audio directory, whatever the
        # database happens to contain.
        if not path.startswith(self.audio_dir + os.sep):
            log.warning("refusing to serve %s: outside %s", path, self.audio_dir)
            return None
        return path if os.path.exists(path) else None


def analyse(path):
    """Characterise the recording, so a clip can be diagnosed without a round
    trip to someone with a signal-processing toolbox.

    Spectral flatness is the useful one: voice sits around 0.01-0.15 because
    formants make the spectrum lumpy, while noise and most digital modes sit
    near 0.5. Crest factor is reported from the pre-limiter peak recorded at
    capture, because the output limiter flattens peaks and makes the figure
    measured from the file meaningless.
    """
    try:
        from scipy.io import wavfile
        from scipy.stats import kurtosis
        sr, a = wavfile.read(path)
        a = a.astype(np.float32) / 32768.0
        if a.ndim > 1:
            a = a.mean(axis=1)
    except Exception as e:
        return f"  (analysis unavailable: {e})\n"
    if a.size < sr // 10:
        return "  (clip too short to analyse)\n"

    fl = max(1, int(0.02 * sr)); nf = a.size // fl
    e = np.sqrt(np.mean(a[:nf * fl].reshape(nf, fl) ** 2, axis=1))
    db = 20 * np.log10(np.maximum(e, 1e-9))
    # Dynamic range separates speech from a steady carrier better than any
    # "how much is active" measure, which needs a threshold that does not
    # generalise across clips. Frames the squelch muted are true digital
    # silence and read as -180 dB, which would put the range in the hundreds;
    # measure against the quietest real audio instead.
    audible = db[db > -120]
    dyn = (float(np.percentile(audible, 90) - np.percentile(audible, 10))
           if audible.size else 0.0)
    muted = int((db <= -120).sum())

    N = 512
    frames = [a[i:i + N] for i in range(0, a.size - N, N // 2)]
    rms = np.array([np.sqrt(np.mean(x ** 2)) for x in frames]) if frames else np.zeros(1)
    loud = [f for f, r in zip(frames, rms) if r > np.percentile(rms, 60)]
    flat, bands = float("nan"), []
    if loud:
        vals = []
        acc = np.zeros(N // 2 + 1)
        for x in loud:
            P = np.abs(np.fft.rfft(x * np.hanning(N))) ** 2
            acc += P
            f = np.fft.rfftfreq(N, 1 / sr); m = (f > 300) & (f < 3400)
            Pm = np.maximum(P[m], 1e-20)
            vals.append(float(np.exp(np.mean(np.log(Pm))) / np.mean(Pm)))
        flat = float(np.median(vals))
        acc /= acc.max() or 1
        f = np.fft.rfftfreq(N, 1 / sr)
        for lo, hi in [(300, 700), (700, 1200), (1200, 1800),
                       (1800, 2500), (2500, 3400)]:
            msk = (f >= lo) & (f < hi)
            bands.append((lo, hi, 10 * np.log10(max(np.mean(acc[msk]), 1e-20))))

    clean = a[np.abs(a) < 0.30]
    kurt = float(kurtosis(clean, fisher=False)) if clean.size > 1000 else float("nan")

    out = [
        f"  length              : {a.size / sr:.2f} s",
        f"  level median / p90  : {np.median(db):.1f} / {np.percentile(db, 90):.1f} dBFS",
        f"  dynamic range       : {dyn:.1f} dB   (speech 15-30, steady tone < 6)",
        (f"  squelch muted       : {muted * 0.02:.2f} s of carrier dropout"
         if muted else ""),
        f"  spectral flatness   : {flat:.3f}   (voice 0.01-0.15, noise ~0.55)",
        f"  kurtosis (unclipped): {kurt:.2f}     (voice > 4, gaussian 3.0)",
        "  spectrum, dB below peak:",
    ]
    out = [x for x in out if x]
    for lo, hi, v in bands:
        out.append(f"    {lo:5d}-{hi:<5d} {v:6.1f}  " + "#" * max(0, int((v + 30) / 1.2)))
    # Deliberately phrased as what dominates, not as what is present. A weak
    # transmission is noise-dominated and still contains perfectly real voice;
    # reading high flatness as "no speech here" is a mistake that costs you
    # a channel worth watching.
    verdict = ("speech-dominated" if flat < 0.2 else
               "noise-dominated -- weak voice may still be present under it"
               if flat > 0.4 else
               "mixed: speech under significant noise")
    out.append(f"  content             : {verdict}")
    return "\n".join(out) + "\n"


def build_bundle(row, audio_file):
    """One zip holding the recording and its transcript.

    A zip rather than two downloads: mobile browsers commonly block the second
    of two downloads from one gesture, and the pair is only useful together --
    the transcript is an index into the audio, not a substitute for it.
    """
    try:
        started = datetime.fromisoformat(row["started_at"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        when = started.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        stem = started.astimezone().strftime("%Y%m%d_%H%M%S")
    except (ValueError, TypeError):
        when, stem = row["started_at"], f"msg{row['id']}"

    slug = re.sub(r"[^A-Za-z0-9]+", "_", row["channel"] or "unknown").strip("_")
    stem = f"{stem}_{slug}"

    def g(key, default=None):
        try:
            return row[key]
        except (IndexError, KeyError):
            return default

    snr = f"{row['snr_db']:.1f} dB" if row["snr_db"] is not None else "unknown"
    text = (row["transcript"] or "").strip()
    if not text:
        text = {"pending": "(not yet transcribed)",
                "backlog": "(recorded, transcription skipped)",
                "error":   "(transcription failed)"}.get(row["status"],
                                                         "(no speech found)")

    def line(label, value):
        return f"  {label:<20}: {value}\n" if value not in (None, "") else ""

    peak = g("peak_pre_limit")
    ngain = g("norm_gain")
    crest = ""
    if peak and ngain:
        # Reconstructed from capture: the stored WAV has been limited, so a
        # crest factor measured from the file understates the real one.
        crest = f"{peak / max(TARGET_RMS, 1e-9):.1f} (pre-limiter)"

    note = (
        f"{wk.STATION_NAME} -- message export\n"
        + "=" * 52 + "\n\n"
        "MESSAGE\n"
        + line("Channel", row["channel"])
        + line("Frequency", f"{(row['freq_hz'] or 0) / 1e6:.3f} MHz")
        + line("Received", when)
        + line("Duration", f"{row['duration_s']:.1f} s")
        + line("Preset", g("preset"))
        + "\nRECEPTION\n"
        + line("Readability", f"{g('quality'):.0f}% (from spectral flatness)"
               if g("quality") is not None else None)
        + line("Carrier", snr + (" below the no-carrier noise reading"
                                 if snr != "unknown" else ""))
        + line("Noise floor", f"{g('noise_floor_db'):.1f} dB"
               if g("noise_floor_db") is not None else None)
        + line("Squelch opened at", f"{g('open_thresh'):.2f}"
               if g("open_thresh") is not None else None)
        + line("Tuner gain", f"{g('gain_db')} dB")
        + line("Peak before limiter", f"{peak:.3f}" if peak else None)
        + line("Normalisation gain", f"{ngain:.2f}x" if ngain else None)
        + line("Crest factor", crest)
        + "\nTRANSCRIPTION\n"
        + line("Confidence", row["status"])
        + line("avg_logprob", f"{g('avg_logprob'):.2f}"
               if g("avg_logprob") is not None else None)
        + line("no_speech_prob", f"{g('no_speech'):.2f}"
               if g("no_speech") is not None else None)
        + line("Real-time factor", f"{g('rtf'):.2f}" if g("rtf") else None)
        + line("Discarded because", g("reject_reason"))
        + line("Decoder said", repr(g("raw_transcript"))
               if g("raw_transcript") and g("raw_transcript") != row["transcript"]
               else None)
        + "\nAUDIO ANALYSIS\n"
        + analyse(audio_file)
        + "\nTRANSCRIPT\n"
        + f"  {text}\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(audio_file, f"{stem}.wav")
        z.writestr(f"{stem}.txt", note)
    return stem, buf.getvalue()


# ---------------------------------------------------------------------------
# Health: SoC temperature, throttle flags, and NPU temperature.

THROTTLE_NOW = [
    (0, "under-voltage"),
    (1, "ARM frequency capped"),
    (2, "throttled"),
    (3, "soft temperature limit"),
]
THROTTLE_EVER = [
    (16, "under-voltage"),
    (17, "ARM frequency capping"),
    (18, "throttling"),
    (19, "soft temperature limit"),
]

_health_cache = {"at": 0.0, "data": {}}
HEALTH_TTL = 20.0        # these move slowly and each read costs a subprocess


def _soc_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def _throttled():
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        bits = int(out.split("=")[1], 16)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
    return {
        "raw": f"0x{bits:x}",
        "now": [name for bit, name in THROTTLE_NOW if bits & (1 << bit)],
        "ever": [name for bit, name in THROTTLE_EVER if bits & (1 << bit)],
    }


# Reading the NPU temperature means briefly opening the Hailo device. If
# something else holds it -- an inference run -- this fails rather than
# interfering, and the panel simply shows the reading as unavailable.
_HAILO_SNIPPET = (
    "import json\n"
    "from hailo_platform import Device\n"
    "d = Device()\n"
    "t = d.control.get_chip_temperature()\n"
    "print(json.dumps(max(t.ts0_temperature, t.ts1_temperature)))\n"
)


def _npu_temp():
    # hailo_platform is installed system-wide by the apt package, so it is not
    # importable from this virtual environment. Ask the system interpreter.
    for python in ("/usr/bin/python3", sys.executable):
        try:
            r = subprocess.run([python, "-c", _HAILO_SNIPPET],
                               capture_output=True, text=True, timeout=8)
            if r.returncode == 0 and r.stdout.strip():
                return round(float(json.loads(r.stdout.strip())), 1)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return None


def health():
    now = time.time()
    if now - _health_cache["at"] < HEALTH_TTL and _health_cache["data"]:
        return _health_cache["data"]
    data = {"soc_c": _soc_temp(), "npu_c": _npu_temp(), "throttle": _throttled()}
    _health_cache.update(at=now, data=data)
    return data


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#101A1F">
<title>{{STATION}}</title>
<style>
  :root {
    --ground:  #101A1F;
    --panel:   #16242B;
    --panel-2: #1B2D35;
    --rule:    #24373F;
    --text:    #DCE7EA;
    --dim:     #7C949D;
    --dimmer:  #55696F;
  }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0;
    background: var(--ground);
    color: var(--text);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    font-size: 16px;
    line-height: 1.45;
    padding-bottom: env(safe-area-inset-bottom);
  }

  header {
    position: sticky; top: 0; z-index: 5;
    background: rgba(16,26,31,.96);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--rule);
    padding: 0 14px 0;
    padding-top: env(safe-area-inset-top);
  }
  summary {
    display: flex; align-items: baseline; gap: 4px 10px;
    padding: 9px 0 8px; cursor: pointer; list-style: none;
    -webkit-tap-highlight-color: transparent;
  }
  summary::-webkit-details-marker { display: none; }
  summary:focus-visible { outline: 2px solid var(--text); outline-offset: -2px; }
  /* Caret turns to point down when the panel is open. */
  .chev {
    width: 8px; height: 8px; flex: none; align-self: center; margin-left: 2px;
    border-right: 1.5px solid var(--dim); border-bottom: 1.5px solid var(--dim);
    transform: rotate(-45deg); transition: transform .15s ease;
  }
  details[open] .chev { transform: rotate(45deg); }
  @media (prefers-reduced-motion: reduce) { .chev { transition: none; } }

  /* Modes that are on while the panel is shut have to stay visible, or the
     log looks quiet when it is only being filtered. */
  .chip {
    font-size: 10.5px; letter-spacing: .04em; color: var(--ground);
    background: var(--dim); border-radius: 3px; padding: 1px 5px;
    align-self: center; flex: none;
  }
  .chip.hot { background: #D9776A; color: #17231F; }

  /* Health sits at the top of the panel: it is the thing you open the panel
     to check when something feels slow. */
  .health {
    display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px;
    font-size: 12.5px; color: var(--dim); margin-bottom: 9px;
    font-variant-numeric: tabular-nums;
  }
  .health b { font-weight: 500; color: var(--text); }
  .health .warm { color: #E0A458; }
  .health .hot  { color: #D9776A; }
  .warn {
    width: 100%; margin: 0 0 9px; padding: 7px 10px; border-radius: 6px;
    font-size: 12.5px; line-height: 1.4;
    background: rgba(217,119,106,.14); color: #E9B4AC;
    border: 1px solid rgba(217,119,106,.35);
  }

  .panel { padding-bottom: 10px; }
  /* Title and live pip are one unit so the pip can never wrap to its own
     line; only the stats give way when the row runs out of width. */
  .brand { display: flex; align-items: baseline; gap: 7px; flex: none; }
  h1 { font-size: 16px; font-weight: 600; letter-spacing: -.01em; margin: 0; }
  .pip { width: 6px; height: 6px; border-radius: 50%; background: #5FB3A8;
         flex: none; }
  .pip.stale { background: #D9776A; }
  .stats { font-size: 12px; color: var(--dim); margin-left: auto;
           font-variant-numeric: tabular-nums; text-align: right;
           min-width: 0; overflow: hidden; white-space: nowrap;
           text-overflow: ellipsis; }

  .controls {
    display: flex; align-items: center; gap: 14px; margin-bottom: 10px;
  }
  .ctl { display: flex; align-items: center; gap: 8px; font-size: 12.5px;
         color: var(--dim); }
  .ctl label { cursor: pointer; user-select: none; white-space: nowrap; }
  .threshold { min-width: 96px; flex: 1 1 auto; }
  .threshold input[type=range] {
    -webkit-appearance: none; appearance: none; background: transparent;
    width: 100%; margin: 0; height: 22px; cursor: pointer;
  }
  .threshold input[type=range]::-webkit-slider-runnable-track {
    height: 3px; background: var(--rule); border-radius: 2px;
  }
  .threshold input[type=range]::-moz-range-track {
    height: 3px; background: var(--rule); border-radius: 2px;
  }
  .threshold input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none; appearance: none;
    width: 18px; height: 18px; border-radius: 50%;
    background: var(--text); border: none; margin-top: -7.5px;
  }
  .threshold input[type=range]::-moz-range-thumb {
    width: 18px; height: 18px; border-radius: 50%;
    background: var(--text); border: none;
  }
  .threshold input:focus-visible { outline: 2px solid var(--text);
                                   outline-offset: 4px; border-radius: 4px; }
  .cut { font-variant-numeric: tabular-nums; color: var(--text);
         white-space: nowrap; flex: none; }
  .cut.off { color: var(--dimmer); }

  /* Toggles state what they will show you now, not what they would do. */
  .toggle {
    font: inherit; font-size: 12.5px; color: var(--dim);
    background: var(--panel); border: 1px solid var(--rule);
    border-radius: 999px; padding: 5px 12px; cursor: pointer;
    white-space: nowrap; flex: none;
    -webkit-tap-highlight-color: transparent;
  }
  .toggle[aria-pressed="true"] {
    background: var(--panel-2); border-color: var(--dim); color: var(--text);
  }
  .toggle:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }

  main { padding: 4px 0 8px; }

  .row {
    display: grid;
    grid-template-columns: 3px 56px 1fr auto;
    gap: 0 11px;
    align-items: start;
    padding: 9px 14px 10px 11px;
    border-bottom: 1px solid var(--rule);
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  .row:focus-visible { outline: 2px solid var(--text); outline-offset: -2px; }
  .row.playing { background: var(--panel); }

  /* The colour bar is the channel identifier, not decoration. */
  .bar { align-self: stretch; border-radius: 2px; min-height: 34px; }

  .age {
    font-size: 20px; font-weight: 500; letter-spacing: -.02em;
    font-variant-numeric: tabular-nums; font-feature-settings: "tnum" 1;
    color: var(--text); padding-top: 1px;
  }
  .row.old .age { color: var(--dim); }
  .meta { font-size: 11.5px; color: var(--dimmer); font-variant-numeric: tabular-nums; }

  .chan { font-size: 12.5px; color: var(--dim); margin-bottom: 2px; }
  /* Signal quality sits with the channel name because the two answer one question
     together: which channel, and was it strong enough to trust. */
  .snr { color: var(--dimmer); font-variant-numeric: tabular-nums;
         margin-left: 7px; }
  .snr.weak { color: #B98A4E; }
  /* Distress vocabulary is never presented as fact. A real call and a
     hallucinated one are identical in text; both need someone to listen. */
  .verify {
    display: inline-block; margin-left: 7px; padding: 1px 6px;
    border-radius: 3px; font-size: 10.5px; letter-spacing: .03em;
    background: #8C3B32; color: #F4DDD9; vertical-align: 1px;
  }
  .row.flagged { background: rgba(140,59,50,.10); }
  .row.flagged .bar { background: #D9776A !important; }
  .text { font-size: 15px; line-height: 1.35; }
  .text.quiet { color: var(--dimmer); font-style: italic; }
  .text.low::after {
    content: " (unclear)"; color: var(--dimmer); font-style: italic;
    font-size: 13px;
  }

  /* Quiet until wanted: dim by default, and given a wide tap area through
     padding rather than through size, so it does not compete with the text. */
  .dl {
    align-self: center; flex: none; display: grid; place-content: center;
    width: 38px; height: 38px; margin: -8px -8px -8px 0;
    color: var(--dimmer); border-radius: 8px;
    -webkit-tap-highlight-color: transparent;
  }
  .dl:hover, .dl:focus-visible { color: var(--text); background: var(--panel-2); }
  .dl:focus-visible { outline: 2px solid var(--text); outline-offset: -2px; }
  .dl svg { width: 15px; height: 15px; display: block; }

  .empty { padding: 64px 24px; text-align: center; color: var(--dim); }
  .docs {
    padding: 22px 16px 34px; text-align: center;
    border-top: 1px solid var(--line); margin-top: 6px;
  }
  .docs a { color: var(--dim); font-size: 13px; text-decoration: none;
            letter-spacing: .02em; }
  .docs a:hover { color: var(--text); text-decoration: underline; }

  /* Channel list inside the panel: one tappable row each, wide enough to hit
     with a thumb, two columns when there is room. */
  .legend {
    border-top: 1px solid var(--rule); padding-top: 8px;
    display: grid; grid-template-columns: 1fr; gap: 1px 18px;
  }
  @media (min-width: 520px) { .legend { grid-template-columns: 1fr 1fr; } }
  .key { display: flex; align-items: baseline; gap: 8px; font-size: 12.5px;
         color: var(--dim); background: none; border: 0; padding: 7px 2px;
         font-family: inherit; cursor: pointer; text-align: left;
         -webkit-tap-highlight-color: transparent; }
  .key:focus-visible { outline: 2px solid var(--text); outline-offset: 2px;
                       border-radius: 4px; }
  /* A muted channel reads as switched off rather than merely quiet: the
     swatch hollows out and the name is struck through. */
  .key.muted { color: var(--dimmer); }
  .key.muted .name { text-decoration: line-through; }
  .key.muted .swatch { background: none !important;
                       box-shadow: inset 0 0 0 1.5px var(--dimmer); }
  .key.muted .freq { color: var(--dimmer); }
  .swatch { width: 10px; height: 10px; border-radius: 2px; flex: none;
            align-self: center; }
  .freq { color: var(--text); font-variant-numeric: tabular-nums; }
  .unit { color: var(--dimmer); font-size: 11px; margin-left: -4px; }
  .count { color: var(--dimmer); font-variant-numeric: tabular-nums;
           margin-left: auto; }
  .key.silent { opacity: .55; }

  @media (prefers-reduced-motion: no-preference) {
    .row { transition: background .12s ease; }
  }
</style>
</head>
<body>
<header>
  <details id="panel">
    <summary>
      <span class="brand"><h1>{{STATION}}</h1><span class="pip" id="pip"></span></span>
      <span class="chip hot" id="hotChip" hidden>THROTTLED</span>
      <span class="chip" id="autoChip" hidden>AUTO</span>
      <span class="stats" id="stats">connecting</span>
      <span class="chev"></span>
    </summary>
    <div class="panel">
      <div class="health" id="health"></div>
      <div id="warnings"></div>
      <div class="controls">
        <div class="ctl threshold">
          <input type="range" id="snrCut" min="0" max="90" step="5" value="0"
                 aria-label="Minimum signal-to-noise ratio to show">
          <span class="cut off" id="cutLabel">Quality all</span>
        </div>
        <button class="toggle" id="hideEmpty" aria-pressed="false">Show All</button>
        <button class="toggle" id="autoplay" aria-pressed="false">AutoPlay Off</button>
      </div>
      <div class="legend" id="legend"></div>
    </div>
  </details>
</header>

<main id="log"><div class="empty">Listening. Nothing heard yet.</div></main>

<div class="docs"><a href="/docs">Operator and maintenance guide</a></div>

<audio id="player" preload="none"></audio>

<script>
const log = document.getElementById('log');
const legend = document.getElementById('legend');
const player = document.getElementById('player');
const pip = document.getElementById('pip');
const statsEl = document.getElementById('stats');
const snrCut = document.getElementById('snrCut');
const cutLabel = document.getElementById('cutLabel');
const hideEmptyBtn = document.getElementById('hideEmpty');
const autoplayBtn = document.getElementById('autoplay');
const autoChip = document.getElementById('autoChip');
const panel = document.getElementById('panel');
const healthEl = document.getElementById('health');
const warnEl = document.getElementById('warnings');
const hotChip = document.getElementById('hotChip');

let msgs = [], shown = [], channels = [], colors = {},
    skew = 0, playing = null, lastOk = 0;
let hideEmpty = false, autoplay = false;
let muted = new Set();            // channels switched off from the legend
let seenIds = null;               // ids known at the last poll
let playQueue = [];

// Settings survive a reload: phone browsers discard and restore background
// tabs freely and re-setting them every time would be tedious.
try {
  const s = JSON.parse(localStorage.getItem('radiolog.filters') || '{}');
  if (typeof s.snr === 'number') snrCut.value = s.snr;
  hideEmpty = !!s.hideEmpty;
  autoplay = !!s.autoplay;
  if (Array.isArray(s.muted)) muted = new Set(s.muted);
  if (s.panelOpen) panel.open = true;      // shut by default: normal operation
} catch (e) { /* first run, or storage unavailable */ }

function saveFilters() {
  try {
    localStorage.setItem('radiolog.filters', JSON.stringify({
      snr: +snrCut.value, hideEmpty, autoplay, muted: [...muted],
      panelOpen: panel.open }));
  } catch (e) { /* private mode; settings just will not persist */ }
}

// Raspberry Pi 5 starts pulling the clock back around 80 C and hard-throttles
// at 85. The NPU runs its own limits, but anything past 80 is worth seeing.
function tempClass(c) { return c >= 80 ? 'hot' : c >= 70 ? 'warm' : ''; }

function drawHealth(h) {
  if (!h) return;
  const bits = [];
  const t = (label, c) => c === null || c === undefined
    ? `${label} <b>--</b>`
    : `${label} <b class="${tempClass(c)}">${c.toFixed(1)}&deg;C</b>`;
  bits.push(t('CPU', h.soc_c));
  bits.push(h.npu_c === null || h.npu_c === undefined
    ? 'NPU <b>not reporting</b>' : t('NPU', h.npu_c));
  if (h.throttle) bits.push(`flags <b>${esc(h.throttle.raw)}</b>`);
  healthEl.innerHTML = bits.join('');

  const now = (h.throttle && h.throttle.now) || [];
  const ever = (h.throttle && h.throttle.ever) || [];
  let html = '';
  if (now.length) {
    html += `<div class="warn"><b>Throttling right now:</b> ${esc(now.join(', '))}.
      The Pi is running below full speed, so transcription will fall behind.
      Check cooling.</div>`;
  }
  // The "has occurred since boot" flags are deliberately NOT shown. They
  // persist until reboot, so a brief spike hours ago keeps the warning on
  // screen indefinitely -- which invites rebooting to clear a light rather
  // than to fix anything. The raw flags stay in the health line for anyone
  // who wants them; only live throttling gets a warning.
  warnEl.innerHTML = html;

  // Visible with the panel shut: a throttle warning hidden behind a collapsed
  // panel is a warning nobody sees.
  hotChip.hidden = !now.length;
}

function paintToggles() {
  hideEmptyBtn.textContent = hideEmpty ? 'Hide No Speech' : 'Show All';
  hideEmptyBtn.setAttribute('aria-pressed', hideEmpty);
  autoplayBtn.textContent = autoplay ? 'AutoPlay On' : 'AutoPlay Off';
  autoplayBtn.setAttribute('aria-pressed', autoplay);
  // Visible with the panel shut, because autoplay makes noise on its own and
  // you should not have to open the panel to find out why.
  autoChip.hidden = !autoplay;
}

const noSpeech = m => !m.text && (m.status === 'empty' || m.status === 'error');

// Mirrors DISTRESS_RE in the watchkeeper.
const DISTRESS = /\bmay ?day\b|\bpan[ -]?pan\b|\bsecurit[ae]\b|\bseelonce\b|\bdistress\b|\bsinking\b|\babandon(?:ing)? ship\b|\bman overboard\b|\btaking on water\b|\bemergency\b/i;
const flagged = m => !!m.text && DISTRESS.test(m.text);

function applyFilters() {
  const cut = +snrCut.value;
  cutLabel.textContent = cut ? `Quality \u2265 ${cut}%` : 'Quality all';
  cutLabel.classList.toggle('off', cut === 0);
  shown = msgs.filter(m =>
    (m.q === null || m.q === undefined || m.q >= cut) &&
    !(hideEmpty && noSpeech(m)) &&
    !muted.has(m.channel));
}

// Sharing the title line means the full four-part string will not fit on a
// phone, so parts that read zero are dropped. Most of the time that leaves
// just the total, and the others appear exactly when they are worth reading.
function drawStats() {
  if (!lastOk) return;
  const since = (Date.now() / 1000) - lastOk;
  if (since > 20) { statsEl.textContent = `No update for ${ago(since)}`; return; }
  const parts = [`${msgs.length} total`];
  const transcribing = msgs.filter(m => m.status === 'pending').length;
  const empty = msgs.filter(noSpeech).length;
  const filtered = msgs.length - shown.length;
  if (transcribing) parts.push(`${transcribing} transcribing`);
  if (empty)        parts.push(`${empty} no speech`);
  if (filtered)     parts.push(`${filtered} filtered`);
  statsEl.textContent = parts.join('  ');
}

// "0:10" for ten seconds, "1:01" for sixty-one, "1:05:03" past an hour.
function ago(sec) {
  sec = Math.max(0, Math.floor(sec));
  const s = sec % 60, m = Math.floor(sec / 60) % 60, h = Math.floor(sec / 3600);
  const p = n => String(n).padStart(2, '0');
  return h ? `${h}:${p(m)}:${p(s)}` : `${m}:${p(s)}`;
}

function body(m) {
  if (m.text) return { cls: m.status === 'low' ? 'text low' : 'text', txt: m.text };
  if (m.status === 'pending')  return { cls: 'text quiet', txt: 'transcribing' };
  if (m.status === 'backlog')  return { cls: 'text quiet', txt: 'recorded, not transcribed' };
  if (m.status === 'error')    return { cls: 'text quiet', txt: 'transcription failed' };
  return { cls: 'text quiet', txt: 'no speech found' };
}

function render() {
  applyFilters();
  if (!shown.length) {
    log.innerHTML = msgs.length
      ? '<div class="empty">Every message is hidden by the filters above.</div>'
      : '<div class="empty">Listening. Nothing heard yet.</div>';
    drawStats(); drawLegend();
    return;
  }
  log.innerHTML = '';
  for (const m of shown) {
    const el = document.createElement('div');
    el.className = 'row' + (playing === m.id ? ' playing' : '')
                         + (flagged(m) ? ' flagged' : '');
    el.tabIndex = 0;
    el.dataset.id = m.id;
    el.dataset.started = m.started;
    const b = body(m);
    el.innerHTML =
      `<div class="bar" style="background:${colors[m.channel] || '#55696F'}"></div>
       <div><div class="age"></div><div class="meta">${m.duration.toFixed(1)}s</div></div>
       <div><div class="chan">${esc(m.channel)}${snr(m)}${
            flagged(m) ? '<span class="verify">LISTEN</span>' : ''}</div>
            <div class="${b.cls}">${esc(b.txt)}</div></div>
       <a class="dl" href="/download/${m.id}" download
          title="Download recording and transcript"
          aria-label="Download recording and transcript">
         <svg viewBox="0 0 16 16" fill="none" stroke="currentColor"
              stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
           <path d="M8 1.5v8.5M4.5 7L8 10.5 11.5 7M2 13.5h12"/>
         </svg></a>`;
    el.addEventListener('click', () => play(m.id));
    // The row plays; the button downloads. Without this the tap would do both.
    el.querySelector('.dl').addEventListener('click', e => e.stopPropagation());
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); play(m.id); }
    });
    log.appendChild(el);
  }
  tick();
}

// Below about 15 dB the transcript is usually wrong because the signal was
// weak rather than because the model failed, so the number is worth a glance.
// Readability, 0-100, from the spectral flatness of the recording. Measured
// against clips whose outcome is known: the ones that decoded correctly sit
// at 84-95, the ones the decoder invented text for at 47-52. Deliberately
// not the carrier strength -- FM capture means a solid carrier at our
// antenna says nothing about what the far end sent.
function snr(m) {
  if (m.q === null || m.q === undefined) return '';
  const cls = m.q < 60 ? 'snr weak' : 'snr';
  return `<span class="${cls}">${m.q}%</span>`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// Ages update every second locally; the server is only polled for new traffic.
function tick() {
  const now = Date.now() / 1000 + skew;
  for (const el of log.children) {
    if (!el.dataset.started) continue;
    const secs = now - parseFloat(el.dataset.started);
    el.querySelector('.age').textContent = ago(secs);
    el.classList.toggle('old', secs > 900);
  }
  const since = (Date.now() / 1000) - lastOk;
  pip.classList.toggle('stale', lastOk > 0 && since > 20);
  drawStats();
}

function drawLegend() {
  // Every watched channel is listed, not just those that have transmitted, so
  // the legend doubles as a statement of what is being received. Counts follow
  // what is on screen so the legend agrees with the list above it.
  const counts = {};
  for (const m of shown) counts[m.channel] = (counts[m.channel] || 0) + 1;
  const rows = channels.slice();
  for (const n of Object.keys(counts)) {
    if (!rows.some(c => c.name === n)) rows.push({ name: n, color: colors[n], mhz: null });
  }
  legend.innerHTML = rows.map(c => {
    const n = counts[c.name] || 0;
    const off = muted.has(c.name);
    const freq = c.mhz === null || c.mhz === undefined ? ''
      : `<span class="freq">${c.mhz.toFixed(3)}</span><span class="unit">MHz</span>`;
    return `<button class="key${off ? ' muted' : (n ? '' : ' silent')}"
      data-chan="${esc(c.name)}" aria-pressed="${!off}">
      <span class="swatch" style="background:${c.color || '#55696F'}"></span>
      <span class="name">${esc(c.name)}</span> ${freq}
      <span class="count">${n}</span></button>`;
  }).join('');
  for (const el of legend.children) {
    el.addEventListener('click', () => {
      const name = el.dataset.chan;
      muted.has(name) ? muted.delete(name) : muted.add(name);
      saveFilters(); render(); drawLegend();
    });
  }
}

function play(id) {
  if (playing === id && !player.paused) {
    player.pause(); playing = null; playQueue = []; render(); return;
  }
  playQueue = [];
  start(id);
}

function start(id) {
  playing = id;
  player.src = `/audio/${id}?t=${Date.now()}`;
  player.play().catch(() => { playing = null; next(); });
  render();
}

function next() {
  const id = playQueue.shift();
  if (id === undefined) { playing = null; render(); return; }
  start(id);
}
player.addEventListener('ended', next);
player.addEventListener('error', next);

// Autoplay queues rather than interrupts: transmissions overlap in time far
// more often than they can be listened to, and cutting one off mid-word to
// start the next would lose both.
function queueNew(fresh) {
  for (const m of fresh) if (!playQueue.includes(m.id)) playQueue.push(m.id);
  if (playing === null) next();
}

async function poll() {
  try {
    const r = await fetch('/api/messages', { cache: 'no-store' });
    const d = await r.json();
    skew = d.now - Date.now() / 1000;
    lastOk = Date.now() / 1000;
    drawHealth(d.health);
    channels = d.channels || [];
    colors = Object.fromEntries(channels.map(c => [c.name, c.color]));

    // Newly arrived messages, oldest first, before any render happens.
    let fresh = [];
    if (seenIds === null) {
      seenIds = new Set(d.messages.map(m => m.id));     // first load: not new
    } else {
      fresh = d.messages.filter(m => !seenIds.has(m.id)).reverse();
      for (const m of d.messages) seenIds.add(m.id);
    }
    const changed = channels.length !== Object.keys(colors).length ||
      d.messages.length !== msgs.length ||
      d.messages.some((m, i) => !msgs[i] || m.id !== msgs[i].id ||
                                m.status !== msgs[i].status ||
                                m.text !== msgs[i].text);
    msgs = d.messages;
    if (changed) { render(); drawLegend(); } else { applyFilters(); drawStats(); }

    if (autoplay && fresh.length) {
      // Autoplay respects the filters: a muted channel or one below the signal
      // cutoff is hidden from the list, so it should not speak either.
      const audible = fresh.filter(m => shown.some(s => s.id === m.id));
      if (audible.length) queueNew(audible);
    }
  } catch (e) {
    stat.textContent = 'offline';
    pip.classList.add('stale');
  }
}

snrCut.addEventListener('input', () => {
  saveFilters(); render(); drawLegend();
});

hideEmptyBtn.addEventListener('click', () => {
  hideEmpty = !hideEmpty;
  paintToggles(); saveFilters(); render(); drawLegend();
});

autoplayBtn.addEventListener('click', () => {
  autoplay = !autoplay;
  if (autoplay) {
    // Mobile browsers only allow audio that a user gesture started. Priming
    // the element inside this click is what makes later autoplay work at all.
    player.muted = true;
    player.play().then(() => {
      player.pause(); player.currentTime = 0; player.muted = false;
    }).catch(() => { player.muted = false; });
  } else {
    playQueue = [];
  }
  paintToggles(); saveFilters();
});

panel.addEventListener('toggle', saveFilters);

paintToggles();
applyFilters();

poll();
setInterval(poll, 3000);
setInterval(tick, 1000);
</script>
</body>
</html>
"""


def page_html():
    """The page with the station name substituted in.

    Done per request rather than once at import so that the name comes from
    the config file, which is loaded after this module.
    """
    name = html.escape(wk.STATION_NAME or "Radio Log")
    return PAGE.replace("{{STATION}}", name)


class Handler(BaseHTTPRequestHandler):
    server_version = "watchkeeper"
    data = None
    preset = None

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/":
            self._send(200, page_html().encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/messages":
            try:
                messages = self.data.messages()
                preset = active_preset(messages, self.preset)
                payload = {
                    "now": time.time(),
                    "health": health(),
                    "preset": preset,
                    "channels": channel_info(preset),
                    "messages": messages,
                }
            except sqlite3.Error as e:
                self._send(503, json.dumps({"error": str(e)}).encode(),
                           "application/json")
                return
            self._send(200, json.dumps(payload).encode(), "application/json")
            return

        m = re.fullmatch(r"/audio/(\d+)", path)
        if m:
            self._serve_audio(int(m.group(1)))
            return

        m = re.fullmatch(r"/download/(\d+)", path)
        if m:
            self._serve_bundle(int(m.group(1)))
            return

        if path in ("/docs", "/docs.html"):
            self._serve_docs()
            return

        self._send(404, b"not found", "text/plain")

    # Looked for beside this script, so the pair deploy together.
    DOCS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "docs.html")

    def _serve_docs(self):
        try:
            with open(self.DOCS_PATH, "rb") as f:
                body = f.read()
        except OSError:
            self._send(404, b"docs.html is not installed beside webui.py",
                       "text/plain; charset=utf-8")
            return
        self._send(200, body, "text/html; charset=utf-8")

    def _serve_bundle(self, msg_id):
        row = self.data.message(msg_id)
        audio = self.data.audio_path(msg_id)
        if not row or not audio:
            self._send(404, b"no recording", "text/plain")
            return
        stem, blob = build_bundle(row, audio)
        self._send(200, blob, "application/zip",
                   {"Content-Disposition": f'attachment; filename="{stem}.zip"'})

    def _serve_audio(self, msg_id):
        path = self.data.audio_path(msg_id)
        if not path:
            self._send(404, b"no recording", "text/plain")
            return
        ctype = mimetypes.guess_type(path)[0] or "audio/wav"
        size = os.path.getsize(path)

        # Safari on iOS will not play audio unless the server honours Range,
        # so this is required rather than an optimisation.
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng:
            mr = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if mr:
                s, e = mr.groups()
                if s:
                    start = min(int(s), size - 1)
                    end = min(int(e), size - 1) if e else size - 1
                elif e:                       # suffix range: last N bytes
                    start = max(0, size - int(e))
                partial = True

        with open(path, "rb") as f:
            f.seek(start)
            body = f.read(end - start + 1)

        headers = {"Accept-Ranges": "bytes"}
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._send(206 if partial else 200, body, ctype, headers)


def main():
    ap = argparse.ArgumentParser(description="Watchkeeper web interface")
    ap.add_argument("--config", metavar="PATH")
    ap.add_argument("--preset", help="restrict the legend to one preset's channels")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", stream=sys.stdout)

    cfg = wk.find_config(args.config)
    if cfg:
        wk.load_config(cfg)
        log.info("configuration: %s", cfg)

    db = wk.DB_PATH
    if not os.path.exists(db):
        log.warning("no database at %s yet; the page will show an empty log "
                    "until the watchkeeper records something", db)

    Handler.data = Data(db, wk.AUDIO_DIR)
    Handler.preset = args.preset

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    log.info("serving on http://%s:%d  (database %s)", args.host, args.port, db)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        srv.shutdown()


if __name__ == "__main__":
    main()
