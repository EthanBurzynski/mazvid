# maz.vid

Compress, trim, mute or mp3-ify videos so they fit on Discord.

## Windows

Download `mazvid.exe` from the [latest release](https://github.com/EthanBurzynski/mazvid/releases/latest) and run it. ffmpeg is built in, nothing else to install.

## Linux

Run from source (uses your system ffmpeg):

```bash
sudo apt install python3-tk ffmpeg      # or your distro's equivalent
pip install -r requirements.txt
python3 mazvid.py
```

Or build a standalone binary with ffmpeg baked in: put static `ffmpeg` and `ffprobe`
binaries (e.g. from https://johnvansickle.com/ffmpeg/) next to `mazvid.spec`, then

```bash
pip install -r requirements.txt pyinstaller
pyinstaller mazvid.spec                  # -> dist/mazvid
```

## Building the Windows exe

Put `ffmpeg.exe` and `ffprobe.exe` next to `mazvid.spec`, then `pyinstaller mazvid.spec` -> `dist/mazvid.exe`.
