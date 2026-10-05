#!/bin/bash
# Build the littlelx SD card image for a Raspberry Pi 3B.
#
# Needs (Debian/Ubuntu): gcc-arm-linux-gnueabihf libc6-dev-armhf-cross make
#   flex bison bc libssl-dev git curl dosfstools mtools fdisk
# Output: pi/out/littlelx-sdcard.img(.gz)  - flash this once
#         pi/out/littlelx-pi-update.zip    - later updates over USB:
#                                            littlelx.py --update-pi <zip>
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORK:-$HERE/work}"
OUT="$HERE/out"
CROSS="${CROSS:-arm-linux-gnueabihf-}"
KERNEL_REPO="https://github.com/raspberrypi/linux.git"
KERNEL_REF="${KERNEL_REF:-rpi-6.12.y}"
FW_TAG="${FW_TAG:-1.20260915}"
FW_URL="https://raw.githubusercontent.com/raspberrypi/firmware/$FW_TAG/boot"
OVERLAYS="piscreen piscreen2r pitft35-resistive tinylcd35 disable-bt disable-wifi"
JOBS="$(nproc)"

mkdir -p "$WORK" "$OUT"

VERSION="${VERSION:-$(git -C "$HERE" describe --always --dirty 2>/dev/null || echo dev)-$(date -u +%Y%m%d)}"

echo "== app ($VERSION)"
make -C "$HERE/app" clean >/dev/null
make -C "$HERE/app" CC="${CROSS}gcc" STRIP="${CROSS}strip" \
	CFLAGS="-Os -mcpu=cortex-a53 -mfpu=neon-vfpv4 -mfloat-abi=hard -DLLX_VERSION=\\\"$VERSION\\\"" \
	LDFLAGS="-static"
cp "$HERE/app/littlelx" "$WORK/init"

# Built into the kernel: just the skeleton. The app itself ships as a separate
# initramfs file (littlelx.cpio.gz) so app updates are small.
cat > "$WORK/initramfs.list" <<LIST
dir /dev 0755 0 0
nod /dev/console 0600 0 0 c 5 1
dir /proc 0755 0 0
dir /sys 0755 0 0
dir /tmp 1777 0 0
LIST

echo "== kernel"
K="$WORK/linux"
if [ ! -d "$K" ]; then
	git clone --depth 1 -b "$KERNEL_REF" "$KERNEL_REPO" "$K"
fi
KMAKE=(make -C "$K" ARCH=arm CROSS_COMPILE="$CROSS" -j"$JOBS")
"${KMAKE[@]}" bcm2709_defconfig
( cd "$K" && ARCH=arm scripts/kconfig/merge_config.sh -m .config "$HERE/kernel.fragment" )
echo "CONFIG_INITRAMFS_SOURCE=\"$WORK/initramfs.list\"" >> "$K/.config"
"${KMAKE[@]}" olddefconfig
for sym in FB_TFT_ILI9486 TOUCHSCREEN_ADS7846 SPI_BCM2835 SERIAL_AMBA_PL011 BCM2835_WDT DEVTMPFS; do
	grep -q "^CONFIG_$sym=y" "$K/.config" || { echo "kernel config: CONFIG_$sym is not =y"; exit 1; }
done
"${KMAKE[@]}" zImage dtbs
echo "file /init $WORK/init 0755 0 0" > "$WORK/app.list"
"$K/usr/gen_init_cpio" "$WORK/app.list" | gzip -9 -n > "$WORK/littlelx.cpio.gz"

echo "== firmware ($FW_TAG)"
mkdir -p "$WORK/fw"
for f in bootcode.bin start_cd.elf fixup_cd.dat; do
	[ -s "$WORK/fw/$f" ] || curl -fsSL -o "$WORK/fw/$f" "$FW_URL/$f"
done

echo "== boot partition"
# Root: firmware + config.txt (never updated). a/: the OS, everything an
# update replaces. Updates write the other slot (b/) and flip os_prefix.
B="$WORK/boot"
S="$B/a"
rm -rf "$B"
mkdir -p "$S/overlays"
cp "$WORK/fw/"* "$HERE/boot/config.txt" "$B/"
cp "$HERE/boot/cmdline.txt" "$K/arch/arm/boot/zImage" "$WORK/littlelx.cpio.gz" "$S/"
cp "$K"/arch/arm/boot/dts/broadcom/bcm2710-rpi-3-b{,-plus}.dtb "$S/"
for o in $OVERLAYS; do
	cp "$K/arch/arm/boot/dts/overlays/$o.dtbo" "$S/overlays/"
done
cp "$K/arch/arm/boot/dts/overlays/overlay_map.dtb" "$S/overlays/" 2>/dev/null || true
echo "$VERSION" > "$S/VERSION"

echo "== update bundle"
rm -f "$OUT/littlelx-pi-update.zip"
( cd "$S" && python3 -m zipfile -c "$OUT/littlelx-pi-update.zip" * )

echo "== image"
IMG="$OUT/littlelx-sdcard.img"
SIZE_MB=32
rm -f "$IMG" "$WORK/boot.vfat"
mkfs.vfat -C -n LITTLELX "$WORK/boot.vfat" $(( (SIZE_MB - 1) * 1024 )) >/dev/null
mcopy -s -i "$WORK/boot.vfat" "$B"/* ::/
truncate -s "${SIZE_MB}M" "$IMG"
echo 'start=2048, type=c, bootable' | sfdisk -q "$IMG"
dd if="$WORK/boot.vfat" of="$IMG" bs=1M seek=1 conv=notrunc status=none
gzip -9 -c "$IMG" > "$IMG.gz"

du -sh "$B" "$S/zImage" "$S/littlelx.cpio.gz" "$IMG.gz" "$OUT/littlelx-pi-update.zip"
echo "Done: $IMG"
