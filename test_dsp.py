"""Offline verification of the DSP chain using a synthesized RF signal."""
import sys, types
import numpy as np

# Stub the deps we can't install offline; only the DSP is under test.
sf = types.ModuleType("soundfile"); sf.write = lambda *a, **k: None; sf.read = lambda *a, **k: None
sys.modules["soundfile"] = sf

import watchkeeper as wk

FS = wk.SDR_SAMPLE_RATE
CHUNK = int(FS * wk.CHUNK_SECONDS)
CENTER = wk.PRESETS["marine"]["center_hz"]


def fm_modulate(tone_hz, n, fs, dev=4000.0, amp=1.0, t0=0.0):
    t = t0 + np.arange(n) / fs
    phase = 2 * np.pi * dev / (2 * np.pi * tone_hz) * np.sin(2 * np.pi * tone_hz * t)
    return amp * np.exp(1j * phase)


def build_rf(n_chunks, active):
    """active: {freq_hz: (tone, amp, chunk_start, chunk_end)}"""
    out = []
    for c in range(n_chunks):
        t0 = c * wk.CHUNK_SECONDS
        n = np.arange(CHUNK)
        sig = (np.random.randn(CHUNK) + 1j * np.random.randn(CHUNK)).astype(np.complex64) * 0.01
        for f, (tone, amp, s, e) in active.items():
            if s <= c < e:
                bb = fm_modulate(tone, CHUNK, FS, amp=amp, t0=t0)
                carrier = np.exp(2j * np.pi * (f - CENTER) / FS * (n + c * CHUNK))
                sig += (bb * carrier).astype(np.complex64)
        out.append(sig.astype(np.complex64))
    return out


def test_decimator_lengths():
    taps = np.ones(129) / 129
    d = wk.PolyphaseDecimator(taps, 16)
    for _ in range(4):
        y = d(np.ones(CHUNK, dtype=np.complex64))
        assert len(y) == CHUNK // 16, len(y)
    # steady-state DC gain should be 1.0 with no seam glitch
    assert np.allclose(y.real, 1.0, atol=1e-3), y[:5]
    print("decimator: length + continuity OK")


def test_decimator_seam():
    """A continuous sine split across chunks must not glitch at the boundary."""
    taps = wk.firwin(129, 50_000, fs=FS)
    d = wk.PolyphaseDecimator(taps, 16)
    n = np.arange(CHUNK * 3)
    x = np.exp(2j * np.pi * 5000 / FS * n).astype(np.complex64)
    y = np.concatenate([d(x[i * CHUNK:(i + 1) * CHUNK]) for i in range(3)])
    y = y[20:]  # skip filter warm-up
    step = np.angle(y[1:] * np.conj(y[:-1]))
    assert step.std() < 1e-4, f"phase discontinuity at seam: std={step.std():.2e}"
    print(f"decimator: seam continuity OK (phase step std {step.std():.2e})")


def test_channel_isolation():
    """Ch16 transmitting must appear on Ch16 and not leak into Ch13/Ch14."""
    ch = wk.Channelizer(CENTER, wk.PRESETS["marine"]["channels"])
    rf = build_rf(8, {156_800_000: (900.0, 1.0, 2, 6)})
    per_chan = {c.spec.name: [] for c in ch.receivers}
    idx = 0
    for chunk in rf:
        for rx in ch.receivers:
            base = rx.dec2(rx.dec1(chunk * rx.osc))
            per_chan[rx.spec.name].append(np.mean(np.abs(base) ** 2))
        idx += len(chunk)
    on = {k: 10 * np.log10(np.mean(v[3:5])) for k, v in per_chan.items()}
    ref = on["Ch16"]
    print("  channel powers while Ch16 is keyed (dB rel. Ch16):")
    for name in ["Ch68", "Ch09", "Ch69", "Ch12", "Ch13", "Ch14", "Ch16", "Ch22A"]:
        print(f"    {name:<6} {on[name] - ref:7.1f}")
    worst = max(v - ref for k, v in on.items() if k != "Ch16")
    assert worst < -50, f"adjacent channel rejection only {-worst:.0f} dB"
    print(f"channel isolation: OK (worst neighbour {-worst:.0f} dB down)")


def test_demod_recovers_tone():
    ch = wk.Channelizer(CENTER, [wk.ChannelSpec("Ch16", 156_800_000)])
    rx = ch.receivers[0]
    rf = build_rf(12, {156_800_000: (900.0, 1.0, 0, 12)})
    audio = []
    for chunk in rf:
        _noise, _speech, a = rx.demodulate(chunk)
        audio.append(a)
    a = np.concatenate(audio)[wk.AUDIO_RATE:]  # skip warm-up
    spec = np.abs(np.fft.rfft(a * np.hanning(len(a))))
    peak = np.fft.rfftfreq(len(a), 1 / wk.AUDIO_RATE)[np.argmax(spec)]
    print(f"  recovered tone at {peak:.0f} Hz (expected 900)")
    assert abs(peak - 900) < 20, f"demodulated tone at {peak:.0f} Hz"
    print("fm demodulation: OK")


def test_noise_squelch_metric():
    """No carrier must read high on the noise metric, carrier must read low."""
    ch = wk.Channelizer(CENTER, [wk.ChannelSpec("Ch16", 156_800_000)])
    rx = ch.receivers[0]
    quiet = build_rf(4, {})
    for c in quiet:
        rx.demodulate(c)
    off = rx.last_noise
    keyed = build_rf(4, {156_800_000: (900.0, 1.0, 0, 4)})
    for c in keyed:
        rx.demodulate(c)
    on = rx.last_noise
    print(f"  noise metric: no carrier {off:.2f}, carrier present {on:.2f}")
    assert off > wk.CARRIER_CLOSE_NOISE, f"idle noise {off:.2f} too low"
    assert on < wk.CARRIER_OPEN_NOISE, f"keyed noise {on:.2f} too high"
    print("noise squelch metric: OK")


def test_gate_and_segment():
    """A 1 second burst on Ch16 should produce exactly one message."""
    ch = wk.Channelizer(CENTER, wk.PRESETS["marine"]["channels"])
    rf = build_rf(28, {156_800_000: (900.0, 1.0, 12, 16)})
    msgs, idx = [], 0
    for chunk in rf:
        msgs.extend(ch.process(chunk, idx))
        idx += len(chunk)
    print(f"  produced {len(msgs)} message(s)")
    for m in msgs:
        dur = m.audio.size / wk.AUDIO_RATE
        t = m.start_sample / wk.AUDIO_RATE
        print(f"    {m.channel} start={t:.2f}s dur={dur:.2f}s snr={m.snr_db:.1f}dB")
    assert len(msgs) == 1, f"expected 1 message, got {len(msgs)}"
    m = msgs[0]
    assert m.channel == "Ch16"
    assert 2.0 < m.start_sample / wk.AUDIO_RATE < 3.2, "start timestamp drifted"
    print("gate + segmentation: OK")


def test_continuous_carrier_splits():
    """The NOAA case: the carrier never drops, so audio pauses have to do the
    splitting. Messages must come out near TARGET_MESSAGE_S rather than as
    short fragments, because Whisper pads every clip to a 30 s window and
    transcribes short ones badly."""
    ch = wk.Channelizer(CENTER, [wk.ChannelSpec("WX1", 156_800_000)])
    n_chunks = 360  # 90 seconds
    period, talk = 24, 16  # 4 s of speech, 2 s of silence, repeating

    msgs, idx = [], 0
    for c in range(n_chunks):
        t0 = c * wk.CHUNK_SECONDS
        n = np.arange(CHUNK)
        talking = (c % period) < talk
        bb = fm_modulate(900.0, CHUNK, FS, amp=1.0, t0=t0) if talking \
            else np.ones(CHUNK, dtype=np.complex64)
        carrier = np.exp(2j * np.pi * (156_800_000 - CENTER) / FS * (n + c * CHUNK))
        sig = (bb * carrier).astype(np.complex64)
        sig += (np.random.randn(CHUNK) + 1j * np.random.randn(CHUNK)).astype(np.complex64) * 0.01
        msgs.extend(ch.process(sig, idx))
        idx += CHUNK

    durs = [m.audio.size / wk.AUDIO_RATE for m in msgs]
    print(f"  carrier up for {n_chunks * wk.CHUNK_SECONDS:.0f}s, "
          f"{len(msgs)} message(s), durations "
          f"{', '.join(f'{d:.1f}s' for d in durs[:6])}"
          f"{' ...' if len(durs) > 6 else ''}")
    assert len(msgs) >= 3, (
        f"continuous carrier produced {len(msgs)} message(s); the gate is not "
        "splitting on audio pauses")
    assert all(d < wk.MAX_MESSAGE_S for d in durs), "a message hit the hard cap"
    # Every message except possibly the last should have waited for the target.
    short = [d for d in durs[:-1] if d < wk.TARGET_MESSAGE_S * 0.8]
    assert not short, f"split below the target length: {short}"
    print("continuous-carrier segmentation: OK")


def test_calibration_from_reference():
    """With clean noise on the reference channels, thresholds should land
    close to the compiled-in defaults."""
    wk.CALIBRATION_SECONDS = 2.0
    chans = [wk.ChannelSpec("Ch16", 156_800_000),
             wk.ChannelSpec("ref-a", 156_537_500, voice=False, role="reference"),
             wk.ChannelSpec("ref-b", 156_737_500, voice=False, role="reference")]
    ch = wk.Channelizer(CENTER, chans)
    for i, chunk in enumerate(build_rf(12, {})):
        ch.process(chunk, i * CHUNK)
    cal = ch.calibrator
    rx = ch.receivers[0]
    print(f"  measured idle {cal.idle_noise:.2f} -> open<{rx.open_noise:.2f} "
          f"close>{rx.close_noise:.2f} (defaults {wk.CARRIER_OPEN_NOISE:.2f}/"
          f"{wk.CARRIER_CLOSE_NOISE:.2f})")
    assert cal.calibrated, "calibration never completed"
    assert abs(rx.open_noise - wk.CARRIER_OPEN_NOISE) < 0.12, "drifted from default"
    assert cal.baseline_power_db is not None
    print("calibration from reference: OK")


def test_calibration_refuses_occupied_reference():
    """If something is transmitting on a reference frequency the reading looks
    like a carrier. Calibrating from that would deafen the receiver, so the
    defaults must be kept."""
    wk.CALIBRATION_SECONDS = 2.0
    chans = [wk.ChannelSpec("Ch16", 156_800_000),
             wk.ChannelSpec("ref-a", 156_537_500, voice=False, role="reference")]
    ch = wk.Channelizer(CENTER, chans)
    # Key a transmitter right on the reference frequency.
    for i, chunk in enumerate(build_rf(12, {156_537_500: (900.0, 1.0, 0, 12)})):
        ch.process(chunk, i * CHUNK)
    rx = ch.receivers[0]
    print(f"  reference occupied; Ch16 thresholds remain "
          f"{rx.open_noise:.2f}/{rx.close_noise:.2f}")
    assert not ch.calibrator.calibrated, "calibrated from an occupied reference"
    assert rx.open_noise == wk.CARRIER_OPEN_NOISE, "thresholds were altered"
    print("calibration safety refusal: OK")


def test_antenna_power_alarm():
    """A large drop in reference noise power should raise the antenna alarm."""
    wk.CALIBRATION_SECONDS = 1.0
    chans = [wk.ChannelSpec("Ch16", 156_800_000),
             wk.ChannelSpec("ref-a", 156_537_500, voice=False, role="reference")]
    ch = wk.Channelizer(CENTER, chans)
    for i, chunk in enumerate(build_rf(8, {})):
        ch.process(chunk, i * CHUNK)
    cal = ch.calibrator
    assert cal.calibrated
    before = cal.baseline_power_db
    # Simulate the coax falling off: same random phase, far less power.
    for i, chunk in enumerate(build_rf(8, {})):
        ch.process((chunk * 0.05).astype(np.complex64), (i + 8) * CHUNK)
    print(f"  baseline {before:.1f} dB, alarm raised: {cal.antenna_warned}")
    assert cal.antenna_warned, "antenna power alarm did not fire"
    print("antenna power alarm: OK")


def test_am_adjacent_channel_rejection():
    """Airband is 25 kHz spaced. None of the configured channels happen to be
    25 kHz apart, so inject a strong signal on the adjacent slot that is NOT
    in the preset and confirm the narrower AM filter keeps it out."""
    p = wk.PRESETS["socal_low"]
    center = p["center_hz"]
    ch = wk.Channelizer(center, p["channels"], p["mode"])
    rx = {r.spec.name: r for r in ch.receivers}
    victim = "LAX South Arrivals"          # 124.900
    intruder = 124_875_000                 # 25 kHz below, not in the preset

    for c in range(8):
        n = np.arange(CHUNK)
        t = c * wk.CHUNK_SECONDS + n / FS
        env = 0.5 * (1.0 + 0.8 * np.sin(2 * np.pi * 1200.0 * t))
        sig = (env * np.exp(2j*np.pi*(intruder-center)/FS*(n + c*CHUNK))).astype(np.complex64)
        sig += (np.random.randn(CHUNK) + 1j*np.random.randn(CHUNK)).astype(np.complex64) * 0.004
        for r in ch.receivers:
            r.demodulate(sig)

    leak = rx[victim].last_noise
    on_freq = rx["Palm Springs Approach"].last_noise   # 350 kHz away, reference
    print(f"  intruder 25 kHz off {victim}: leaked carrier {leak:.1f} dBFS, "
          f"far channel {on_freq:.1f} dBFS")
    open_thr, _ = rx[victim]._am_thresholds()
    print(f"  squelch would open above {open_thr:.1f} dBFS")
    assert leak < open_thr, (
        f"adjacent channel leaks at {leak:.1f} dBFS, above the {open_thr:.1f} "
        "dBFS squelch threshold: the filter is too wide")
    print("AM adjacent-channel rejection: OK")


def test_am_demod_and_gate():
    """Aviation AM: a keyed carrier is exactly one message, and the envelope
    detector must recover the modulation."""
    p = wk.PRESETS["socal_low"]
    center = p["center_hz"]
    ch = wk.Channelizer(center, p["channels"], p["mode"])
    ch.calibrator._needed = int(2.0 / wk.CHUNK_SECONDS)
    freq = 124_900_000
    msgs, idx = [], 0
    for c in range(48):
        n = np.arange(CHUNK)
        t = c * wk.CHUNK_SECONDS + n / FS
        sig = (np.random.randn(CHUNK) + 1j * np.random.randn(CHUNK)).astype(np.complex64) * 0.004
        if 20 <= c < 36:
            env = 0.30 * (1.0 + 0.7 * np.sin(2 * np.pi * 900.0 * t))
            sig = sig + (env * np.exp(2j*np.pi*(freq-center)/FS*(n + c*CHUNK))).astype(np.complex64)
        msgs.extend(ch.process(sig.astype(np.complex64), idx))
        idx += CHUNK

    print(f"  noise floor {ch.calibrator.idle_noise:.1f} dBFS, "
          f"{len(msgs)} message(s)")
    assert ch.calibrator.calibrated, "AM calibration never completed"
    assert len(msgs) == 1, f"expected 1 AM message, got {len(msgs)}"
    m = msgs[0]
    dur = m.audio.size / wk.AUDIO_RATE
    print(f"    {m.channel!r} dur={dur:.2f}s snr={m.snr_db:.1f}dB")
    assert m.channel == "LAX South Arrivals", m.channel
    assert 4.0 < dur < 6.0, f"duration {dur:.2f}s"
    spec = np.abs(np.fft.rfft(m.audio * np.hanning(m.audio.size)))
    pk = np.fft.rfftfreq(m.audio.size, 1 / wk.AUDIO_RATE)[np.argmax(spec)]
    print(f"    recovered tone {pk:.0f} Hz (expected 900)")
    assert abs(pk - 900) < 20, f"tone at {pk:.0f} Hz"
    print("AM demodulation + carrier gate: OK")


def test_preset_geometry():
    """Every channel must sit inside the usable capture window and land on a
    frequency offset the precomputed oscillator can represent exactly."""
    edge = 0.4 * wk.SDR_SAMPLE_RATE
    for name, p in sorted(wk.PRESETS.items()):
        center = p["center_hz"]
        worst = max(abs(c.freq_hz - center) for c in p["channels"])
        wk.Channelizer(center, p["channels"], p.get("mode", "nfm"))
        flag = "" if worst <= edge else "  (near filter edge)"
        print(f"  {name:11s} {p.get('mode','nfm'):4s} {len(p['channels']):2d} ch  "
              f"max offset {worst/1e3:6.1f} kHz{flag}")
        assert worst < 0.5 * wk.SDR_SAMPLE_RATE, f"{name}: {worst} Hz is outside the capture"
    print("preset geometry: OK")


def test_bad_offset_rejected():
    try:
        wk.Channelizer(156_762_500, [wk.ChannelSpec("bad", 156_765_001)])
    except ValueError as e:
        print(f"offset guard: OK ({str(e)[:60]}...)")
        return
    raise AssertionError("expected a ValueError for a non-grid offset")


if __name__ == "__main__":
    np.random.seed(0)
    test_decimator_lengths()
    test_decimator_seam()
    test_bad_offset_rejected()
    test_channel_isolation()
    test_demod_recovers_tone()
    test_noise_squelch_metric()
    test_gate_and_segment()
    test_continuous_carrier_splits()
    test_calibration_from_reference()
    test_calibration_refuses_occupied_reference()
    test_antenna_power_alarm()
    test_preset_geometry()
    test_am_demod_and_gate()
    test_am_adjacent_channel_rejection()
    print("\nall DSP tests passed")
