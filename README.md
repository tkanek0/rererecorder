# ReReRecorder

Records an Intel RealSense D455 and a ReSpeaker USB Mic Array side by side, on
one clock, so that sound and geometry can be lined up afterwards from the files.
What it records and how well is in [docs/features.md](docs/features.md).

## Running it

Two halves: the control plane (`rrr.api`, on :8040) and the page (vite, on
:5177). Open the page; it finds the control plane on the same host.

**On Linux, `make up`.** It runs both in containers, because recording there
needs librealsense built for RSUSB ([decisions 1 and 30](docs/decisions.md)).
Once per machine, Docker Engine with the compose plugin, and udev rules so the
container's user can open both devices:

```bash
sudo curl -fsSL https://raw.githubusercontent.com/IntelRealSense/librealsense/v2.58.3/config/99-realsense-libusb.rules \
  -o /etc/udev/rules.d/99-realsense-libusb.rules
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="2886", ATTRS{idProduct}=="0018", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/99-respeaker.rules
sudo udevadm control --reload-rules && sudo udevadm trigger   # then replug both
```

```bash
make image                      # once, ~4 min
make up                         # the page on http://localhost:5177
```

**Elsewhere, natively** - which is how it runs on Windows:

```bash
uv sync
uv run python -m rrr.api        # control plane
npm --prefix web install
npm --prefix web run dev        # the page, on http://localhost:5177
```

The command-line tools - recording, checking, exporting, rendering - are in
[docs/features.md](docs/features.md#the-command-line).

## Where recordings go

Under `data/` at the repository root, on the host and in the container alike. It
is ignored by git and is normally a symbolic link to a disk with room
([decision 28](docs/decisions.md)):

```bash
ln -s /mnt/<disk>/rererecorder data
```

What a session directory holds is in [docs/design.md](docs/design.md#a-session-is-a-directory).

## Documentation

| | |
|---|---|
| [design.md](docs/design.md) | the one idea, module boundaries, what a session is |
| [features.md](docs/features.md) | what it records, what it reports, how to drive it, and what is not done yet |
| [decisions.md](docs/decisions.md) | each choice, the alternatives, and the measurement that decided it |
| [frame-loss.md](docs/frame-loss.md) | the frame-loss investigation on Linux |
| [windows-native.md](docs/windows-native.md) | the investigation on Windows, natively and under WSL2 |

Tests: `uv run pytest`, none needing a device.
