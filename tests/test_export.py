"""The neutral layout: what leaves this repository for anything else to read.

`video.rrdb` is shaped for writing 54 MB/s. Nothing outside this repository
should have to know that, so a session is exported as plain files. The tests
here build a small but complete session - both devices, inertial samples, a
direction track and a mark - and check that the export says what the recording
said, including where it says nothing.
"""

from __future__ import annotations

import csv
import json
import os
import wave
from pathlib import Path

import cv2
import numpy as np
import pytest

from rrr.timeline import (
    AudioClockPoint,
    AudioClockWriter,
    AudioTrack,
    ClockPair,
    Event,
    EventWriter,
    Rig,
    SessionManifest,
    SessionPaths,
    SyncCalibration,
    VideoTrack,
    write_manifest,
)
from rrr.tools import export as exporter
from rrr.video import (
    ArchiveWriter,
    Calibration,
    Extrinsics,
    FrameSet,
    Intrinsics,
    StreamConfig,
)
from rrr.video.types import MotionSample

RATE = 16_000
CHANNELS = 6
WIDTH, HEIGHT = 32, 24
FPS = 30.0
FRAMES = 10
MONO = 1_402_562.0
REAL = 1_788_330_516.0
OFFSET = REAL - MONO
BASELINE_M = 0.095


def _intrinsics() -> Intrinsics:
    return Intrinsics(
        width=WIDTH,
        height=HEIGHT,
        fx=16.0,
        fy=16.0,
        ppx=16.0,
        ppy=12.0,
        model="brown_conrady",
        coeffs=(0.0,) * 5,
    )


def _calibration() -> Calibration:
    from rrr.video.types import MotionCalibration, MotionIntrinsics

    return Calibration(
        color=_intrinsics(),
        depth=_intrinsics(),
        depth_scale=0.001,
        depth_to_color=Extrinsics.identity(),
        aligned=False,
        infrared=(_intrinsics(), _intrinsics()),
        depth_to_infrared=(
            Extrinsics.identity(),
            Extrinsics(
                rotation=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
                translation=(-BASELINE_M, 0.0, 0.0),
            ),
        ),
        motion=MotionCalibration(
            accel=MotionIntrinsics(
                data=tuple(float(v) for v in range(12)),
                noise_variances=(1e-3,) * 3,
                bias_variances=(1e-5,) * 3,
            ),
            gyro=None,
            depth_to_accel=Extrinsics(
                rotation=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
                translation=(-0.03, 0.007, 0.017),
            ),
            depth_to_gyro=None,
        ),
    )


@pytest.fixture
def session(tmp_path: Path) -> SessionPaths:
    """A complete short session: both devices, inertial, direction, one mark."""
    paths = SessionPaths.create(str(tmp_path / "sessions"), "whole")
    calibration = _calibration()
    rng = np.random.default_rng(20260907)

    with ArchiveWriter(
        paths.video, calibration=calibration, config=StreamConfig(infrared=True)
    ) as writer:
        for n in range(FRAMES):
            capture = MONO + n / FPS
            # Two inertial samples per frame, so both streams have rows.
            writer.append_motion(
                [
                    MotionSample(
                        stream=stream,
                        timestamp_ms=(capture + OFFSET) * 1000.0,
                        x=float(n),
                        y=1.0,
                        z=9.8,
                    )
                    for stream in ("accel", "gyro")
                ]
            )
            assert writer.append(
                FrameSet(
                    index=n + 1,
                    color_timestamp_ms=(capture + OFFSET) * 1000.0,
                    depth_timestamp_ms=(capture + OFFSET) * 1000.0,
                    received_monotonic=capture,
                    color=rng.integers(0, 2**16, (HEIGHT, WIDTH), dtype=np.uint16),
                    color_format="yuyv",
                    depth=rng.integers(0, 4000, (HEIGHT, WIDTH), dtype=np.uint16),
                    calibration=calibration,
                    motion=None,
                    timestamp_domain="global_time",
                    infrared=(
                        rng.integers(0, 255, (HEIGHT, WIDTH), dtype=np.uint8),
                        rng.integers(0, 255, (HEIGHT, WIDTH), dtype=np.uint8),
                    ),
                ),
                timeout=30.0,
            )
        assert writer.drain()

    seconds = FRAMES / FPS
    audio = np.zeros((int(seconds * RATE), CHANNELS), dtype="<i2")
    with wave.open(paths.audio, "wb") as out:
        out.setnchannels(CHANNELS)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(audio.tobytes())

    with AudioClockWriter(paths.audio_clock) as clock_writer:
        for block in range(0, len(audio) + 1, RATE // 4):
            clock_writer.append(
                AudioClockPoint(sample=block, monotonic=MONO + block / RATE)
            )

    with open(paths.doa, "w", encoding="utf-8") as handle:
        for n in range(5):
            handle.write(
                json.dumps({"t": MONO + n * 0.06, "angle": 90 + n, "voice": n % 2})
                + "\n"
            )

    with EventWriter(paths.events) as marks:
        marks.append(
            Event(
                monotonic=MONO + 0.1,
                realtime=REAL + 0.1,
                label="speaker 45deg 2m",
                data={"azimuth_deg": 45},
            )
        )

    write_manifest(
        paths,
        SessionManifest(
            session_id="whole",
            started_at=ClockPair(MONO, REAL),
            stopped_at=ClockPair(MONO + seconds, REAL + seconds),
            video=VideoTrack(frames=FRAMES, fps=FPS, timestamp_domain="global_time"),
            audio=AudioTrack(
                rate=RATE, channels=CHANNELS, samples=len(audio), first_monotonic=MONO
            ),
            doa_file="doa.jsonl",
            events_file="events.jsonl",
        ),
    )
    return paths


def _export(session: SessionPaths, tmp_path: Path, **kwargs) -> tuple[Path, dict]:
    """Export a session and return where it went and what the manifest says."""
    from rrr.timeline import read_manifest

    destination = tmp_path / "export" / "whole"
    written = exporter.export(
        session, read_manifest(session), str(destination), **kwargs
    )
    return destination, written


def _rows(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# -- what comes out -----------------------------------------------------------


def test_every_stream_is_written_and_indexed(session, tmp_path) -> None:
    out, manifest = _export(session, tmp_path)

    assert set(manifest["streams"]) == {
        "color",
        "ir_left",
        "ir_right",
        "depth",
        "imu_accel",
        "imu_gyro",
        "audio",
        "doa",
        "events",
    }
    for name in ("color", "ir_left", "ir_right", "depth"):
        assert manifest["streams"][name]["count"] == FRAMES
        assert len(_rows(out / name / "index.csv")) == FRAMES
    assert manifest["streams"]["doa"]["count"] == 5
    assert manifest["streams"]["events"]["count"] == 1


def test_the_manifest_is_the_index(session, tmp_path) -> None:
    """Every path the manifest names exists, and nothing is left unnamed."""
    out, manifest = _export(session, tmp_path)

    for stream in manifest["streams"].values():
        for key in ("index", "data", "file", "clock", "fit"):
            if key in stream:
                assert (out / stream[key]).exists(), stream[key]

    named = {"manifest.json", "calibration.json", "derived"} | {
        entry.split("/")[0] for stream in manifest["streams"].values()
        for key, entry in stream.items()
        if key in ("index", "data", "file", "clock", "fit")
    }
    assert set(os.listdir(out)) == named


def test_times_are_nanoseconds_on_the_recording_axis(session, tmp_path) -> None:
    out, _ = _export(session, tmp_path)

    first = _rows(out / "color" / "index.csv")[0]
    assert int(first["t_ns"]) == pytest.approx(MONO * 1e9, abs=1e6)
    # The filename is the time, so a directory listing sorts into order.
    assert first["file"] == f"data/{first['t_ns']}.png"


def test_depth_keeps_its_sixteen_bits(session, tmp_path) -> None:
    """Written as mm-scaled z16 would lose the scale; it stays raw."""
    out, _ = _export(session, tmp_path)

    row = _rows(out / "depth" / "index.csv")[0]
    image = cv2.imread(str(out / "depth" / row["file"]), cv2.IMREAD_UNCHANGED)
    assert image.dtype == np.uint16

    calibration = json.loads((out / "calibration.json").read_text())
    assert calibration["sensors"]["depth"]["scale_m"] == 0.001


def test_colour_comes_out_as_readable_rgb(session, tmp_path) -> None:
    out, manifest = _export(session, tmp_path)

    row = _rows(out / "color" / "index.csv")[0]
    image = cv2.imread(str(out / "color" / row["file"]), cv2.IMREAD_UNCHANGED)
    assert image.shape == (HEIGHT, WIDTH, 3)
    assert manifest["streams"]["color"]["pixel"] == "rgb8"
    assert manifest["source"]["color_encoding"] == "png"


def test_the_two_inertial_streams_stay_apart(session, tmp_path) -> None:
    """Joining them would mean resampling one onto the other's timestamps."""
    out, manifest = _export(session, tmp_path)

    assert manifest["streams"]["imu_accel"]["units"] == "m/s^2"
    assert manifest["streams"]["imu_gyro"]["units"] == "rad/s"
    assert len(_rows(out / "imu_accel" / "index.csv")) == FRAMES


def test_the_audio_is_copied_whole_with_its_time_mapping(session, tmp_path) -> None:
    out, manifest = _export(session, tmp_path)

    with wave.open(str(out / "audio" / "audio.wav")) as handle:
        assert handle.getnchannels() == CHANNELS
        assert handle.getframerate() == RATE

    assert manifest["streams"]["audio"]["clock_points"] > 1
    clock = _rows(out / "audio" / "clock.csv")
    assert clock[0]["sample"] == "0"
    fit = json.loads((out / "audio" / "clock_fit.json").read_text())
    assert fit["points"] == len(clock)


def test_a_mark_keeps_its_label_and_its_extras(session, tmp_path) -> None:
    out, _ = _export(session, tmp_path)

    row = _rows(out / "events" / "index.csv")[0]
    assert row["label"] == "speaker 45deg 2m"
    assert json.loads(row["data"]) == {"azimuth_deg": 45}


# -- the calibration ----------------------------------------------------------


def test_transforms_name_both_frames(session, tmp_path) -> None:
    """A list rather than nesting, so a different rig is the same shape."""
    out, _ = _export(session, tmp_path)
    calibration = json.loads((out / "calibration.json").read_text())

    assert calibration["reference_frame"] == "depth"
    pairs = {(entry["from"], entry["to"]) for entry in calibration["extrinsics"]}
    assert pairs == {
        ("depth", "color"),
        ("depth", "ir_left"),
        ("depth", "ir_right"),
        ("depth", "imu_accel"),
    }
    assert calibration["infrared_baseline_m"] == pytest.approx(BASELINE_M)


def test_a_sensor_the_device_did_not_report_is_simply_absent(
    session, tmp_path
) -> None:
    """The gyro has no calibration here, which must not become an identity."""
    out, _ = _export(session, tmp_path)
    calibration = json.loads((out / "calibration.json").read_text())

    assert "imu_gyro" not in calibration["sensors"]
    assert ("depth", "imu_gyro") not in {
        (entry["from"], entry["to"]) for entry in calibration["extrinsics"]
    }


def test_an_unset_rig_is_exported_as_unset_and_noted(session, tmp_path) -> None:
    out, manifest = _export(session, tmp_path)
    calibration = json.loads((out / "calibration.json").read_text())

    assert calibration["array"]["source"] == "unset"
    assert calibration["array"]["to_depth"] is None
    assert any("mounting" in note for note in manifest["notes"])
    assert any("unmeasured" in note for note in manifest["notes"])


def test_a_filled_in_rig_reaches_the_export(session, tmp_path) -> None:
    from rrr.timeline import read_manifest

    manifest = read_manifest(session).with_rig(
        Rig(
            source="nominal",
            rotation=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
            translation=(0.0, -0.05, 0.0),
            microphones=((0.0463, 0.0, 0.0), (0.0, 0.0463, 0.0)),
            channels=(1, 2),
            description="array on top of the camera",
        )
    )
    write_manifest(session, manifest)

    out, written = _export(session, tmp_path)
    calibration = json.loads((out / "calibration.json").read_text())

    assert calibration["array"]["source"] == "nominal"
    assert calibration["array"]["to_depth"]["from"] == "array"
    assert calibration["array"]["to_depth"]["to"] == "depth"
    assert len(calibration["array"]["microphones"]) == 2
    assert not any("mounting" in note for note in written["notes"])


def test_the_device_offset_is_written_but_not_applied(session, tmp_path) -> None:
    """Applying it would bake one alignment into files meant to outlast it."""
    from rrr.timeline import read_manifest

    write_manifest(
        session,
        read_manifest(session).with_calibration(
            SyncCalibration(offset_s=0.08, method="handclap")
        ),
    )

    out, written = _export(session, tmp_path)
    calibration = json.loads((out / "calibration.json").read_text())

    assert calibration["time_offset_s"]["value"] == 0.08
    assert not any("unmeasured" in note for note in written["notes"])
    # The audio's own times are untouched by it.
    assert _rows(out / "audio" / "clock.csv")[0]["t_ns"] == str(
        exporter._ns(MONO)
    )


# -- choosing what to write ---------------------------------------------------


def test_a_stride_writes_every_nth_frame(session, tmp_path) -> None:
    out, manifest = _export(session, tmp_path, stride=3)

    assert manifest["streams"]["color"]["count"] == 4
    assert len(_rows(out / "color" / "index.csv")) == 4
    assert manifest["source"]["frames"]["stride"] == 3


def test_a_range_writes_only_that_range(session, tmp_path) -> None:
    out, manifest = _export(session, tmp_path, start=2, end=5)

    rows = _rows(out / "color" / "index.csv")
    assert [int(row["frame"]) for row in rows] == [2, 3, 4]


def test_jpeg_is_offered_and_recorded_as_lossy(session, tmp_path) -> None:
    out, manifest = _export(session, tmp_path, color="jpeg")

    assert manifest["streams"]["color"]["encoding"] == "jpeg"
    row = _rows(out / "color" / "index.csv")[0]
    assert row["file"].endswith(".jpg")
    # Depth and infrared are never lossy, whatever colour is asked for.
    assert _rows(out / "depth" / "index.csv")[0]["file"].endswith(".png")
    assert _rows(out / "ir_left" / "index.csv")[0]["file"].endswith(".png")


def test_a_session_with_only_video_exports(tmp_path) -> None:
    """One device failing is a recorded outcome, not a reason to refuse."""
    paths = SessionPaths.create(str(tmp_path / "sessions"), "videoonly")
    calibration = _calibration()
    with ArchiveWriter(
        paths.video, calibration=calibration, config=StreamConfig()
    ) as writer:
        assert writer.append(
            FrameSet(
                index=1,
                color_timestamp_ms=None,
                depth_timestamp_ms=REAL * 1000.0,
                received_monotonic=MONO,
                color=None,
                motion=None,
                depth=np.zeros((HEIGHT, WIDTH), np.uint16),
                calibration=calibration,
                timestamp_domain="global_time",
            ),
            timeout=30.0,
        )
        assert writer.drain()
    write_manifest(
        paths,
        SessionManifest(session_id="videoonly", video=VideoTrack(frames=1)),
    )

    from rrr.timeline import read_manifest

    destination = tmp_path / "export" / "videoonly"
    written = exporter.export(paths, read_manifest(paths), str(destination))

    assert set(written["streams"]) == {"depth"}
    assert not (destination / "audio").exists()
