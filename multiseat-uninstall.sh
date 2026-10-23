#!/bin/bash
# ==============================================================================
# MultiSeat Mint Uninstaller & Display Reset Script
#
# Reverts all configurations, services, udev rules, and display settings
# modified by multiseat-mint.sh / multiseat.sh back to system defaults.
#
# Can be run from:
#   1. Recovery mode root shell
#   2. User terminal (automatically elevates via sudo)
#   3. Anywhere in PATH if installed (multiseat-uninstall)
# ==============================================================================

set -o pipefail

# ANSI color codes
RED="\e[1;31m"
GREEN="\e[1;32m"
YELLOW="\e[1;33m"
CYAN="\e[1;36m"
BOLD="\e[1m"
RESET="\e[0m"

log_info()    { echo -e "${CYAN}[INFO]${RESET} $1"; }
log_ok()      { echo -e "${GREEN}[OK]${RESET} $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${RESET} $1"; }
log_error()   { echo -e "${RED}[ERROR]${RESET} $1"; }
log_section() { echo -e "\n${BOLD}${CYAN}=== $1 ===${RESET}"; }

# ------------------------------------------------------------------------------
# 1. Self-installation helper (--install)
# ------------------------------------------------------------------------------
if [[ "$1" == "--install" ]]; then
    if [ "$EUID" -ne 0 ]; then
        echo -e "${YELLOW}Installing requires root. Elevating with sudo...${RESET}"
        exec sudo "$0" "$@"
    fi

    SCRIPT_SRC="$(readlink -f "$0")"
    TARGET_BIN="/usr/local/bin/multiseat-uninstall"
    TARGET_SBIN="/usr/local/sbin/multiseat-uninstall"

    install -m 755 "$SCRIPT_SRC" "$TARGET_BIN"
    ln -sf "$TARGET_BIN" "$TARGET_SBIN"

    echo -e "${GREEN}[OK]${RESET} Installed successfully to:"
    echo "  - $TARGET_BIN"
    echo "  - $TARGET_SBIN"
    echo "You can now run 'multiseat-uninstall' from any shell or recovery mode."
    exit 0
fi

# ------------------------------------------------------------------------------
# 2. Privilege & Filesystem Verification
# ------------------------------------------------------------------------------
# Auto-elevate to root if run by normal user
if [ "$EUID" -ne 0 ]; then
    log_warn "This script requires root privileges. Elevating with sudo..."
    exec sudo "$0" "$@"
fi

# In recovery mode, root is often mounted read-only. Ensure read-write access.
if ! touch /etc/.rw_test 2>/dev/null; then
    log_warn "Root filesystem is read-only. Remounting as read-write..."
    mount -o remount,rw / || {
        log_error "Failed to remount root filesystem as read-write! Aborting."
        exit 1
    }
    log_ok "Root filesystem remounted as read-write."
else
    rm -f /etc/.rw_test
fi

PURGE=false
REBOOT_PROMPT=false
for arg in "$@"; do
    case "$arg" in
        --purge)
            PURGE=true
            ;;
        --reboot)
            REBOOT_PROMPT=true
            ;;
        -h|--help)
            echo "Usage: multiseat-uninstall [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --install   Install this script into /usr/local/bin & /usr/local/sbin"
            echo "  --purge     Also remove built binaries and /home/multiseat source tree"
            echo "  --reboot    Reboot the system immediately upon completion"
            echo "  -h, --help  Show this help message"
            exit 0
            ;;
    esac
done

echo -e "${BOLD}${RED}"
echo "=========================================================="
echo "      MULTISEAT UNINSTALLER & DISPLAY RESTORER            "
echo "=========================================================="
echo -e "${RESET}"
log_info "Starting cleanup to restore Linux Mint default single-seat display..."

# ------------------------------------------------------------------------------
# 3. Stop running multiseat services and processes
# ------------------------------------------------------------------------------
log_section "Stopping Active Processes & Services"

# Stop systemd units if systemd is running
if command -v systemctl >/dev/null 2>&1; then
    log_info "Stopping multiseat units..."
    systemctl stop multiseat.service 2>/dev/null || true
    systemctl stop "multiseat-*" 2>/dev/null || true
    systemctl stop "dlm-*" 2>/dev/null || true
    systemctl reset-failed 2>/dev/null || true
fi

# Terminate any stray processes
log_info "Terminating any lingering multiseat / DRM lease / compositor processes..."
pkill -9 -f "drm-lease-manager" 2>/dev/null || true
pkill -9 -f "multiseat-mint.sh" 2>/dev/null || true
pkill -9 -f "multiseat.sh" 2>/dev/null || true
pkill -9 -f "multiseat-gui" 2>/dev/null || true
pkill -9 -f "sfwbar" 2>/dev/null || true

log_ok "Processes stopped."

# ------------------------------------------------------------------------------
# 4. Remove Systemd Services & Environment Overrides
# ------------------------------------------------------------------------------
log_section "Restoring Systemd Configuration & Display Target"

if command -v systemctl >/dev/null 2>&1; then
    # Disable multiseat service
    systemctl disable multiseat.service 2>/dev/null || true

    # Remove unit files
    rm -f /etc/systemd/system/multiseat.service
    rm -f /etc/systemd/system/multi-user.target.wants/multiseat.service
    rm -f /etc/systemd/system/graphical.target.wants/multiseat.service

    # Unset global environment variables injected by multiseat
    systemctl unset-environment \
        SEATD_VTBOUND \
        XDG_SESSION_TYPE \
        XKB_DEFAULT_LAYOUT \
        XDG_SEAT \
        DRM_LEASE \
        usbdvs \
        open 2>/dev/null || true

    # Revert system default target to graphical.target (Linux Mint standard)
    log_info "Reverting default boot target to 'graphical.target'..."
    systemctl set-default graphical.target 2>/dev/null || true

    # Ensure display manager (lightdm) is enabled and unmasked
    for dm in lightdm gdm3 gdm sddm; do
        if command -v "$dm" >/dev/null 2>&1 || systemctl list-unit-files "$dm.service" 2>/dev/null | grep -q "$dm"; then
            log_info "Unmasking and enabling $dm.service..."
            systemctl unmask "$dm.service" 2>/dev/null || true
            systemctl enable "$dm.service" 2>/dev/null || true
            break
        fi
    done

    # Ensure getty@tty1 is enabled
    systemctl enable getty@tty1.service 2>/dev/null || true

    systemctl daemon-reload 2>/dev/null || true
    log_ok "Systemd units and targets restored."
fi

# ------------------------------------------------------------------------------
# 5. Revert Udev Rules & Flush Logind Seats
# ------------------------------------------------------------------------------
log_section "Reverting Udev Rules & Seat Allocations"

# Revert modified 71-seat.rules
for seat_rules in /usr/lib/udev/rules.d/71-seat.rules /lib/udev/rules.d/71-seat.rules; do
    if [ -f "$seat_rules" ]; then
        if grep -q 'TAG+="seat", TAG+="master-of-seat"' "$seat_rules"; then
            log_info "Restoring $seat_rules (removing master-of-seat from input devices)..."
            sed -i 's/SUBSYSTEM=="input", KERNEL=="input\*", TAG+="seat", TAG+="master-of-seat"/SUBSYSTEM=="input", KERNEL=="input*", TAG+="seat"/' "$seat_rules"
            log_ok "Restored $seat_rules"
        fi
    fi
done

# Remove any dynamic or attached seat rules written to /etc/udev/rules.d
log_info "Cleaning /etc/udev/rules.d seat rules..."
rm -f /etc/udev/rules.d/72-seat-*.rules
rm -f /etc/udev/rules.d/*multiseat*.rules

# Flush logind attached devices so all hardware returns to seat0
if command -v loginctl >/dev/null 2>&1; then
    log_info "Flushing secondary seats back to seat0..."
    loginctl flush-devices 2>/dev/null || true
fi

# Reload udev rules and trigger hardware re-evaluation
if command -v udevadm >/dev/null 2>&1; then
    log_info "Reloading udev rules and triggering hardware scan..."
    udevadm control --reload 2>/dev/null || true
    udevadm trigger 2>/dev/null || true
fi
log_ok "Udev and seats successfully restored to default (seat0)."

# ------------------------------------------------------------------------------
# 6. Remove Auto-Generated Multiseat Users (e.g. u0-vga*, u0-hdmi*)
# ------------------------------------------------------------------------------
log_section "Cleaning Up Auto-Generated Seat Users"

# Match usernames generated by multiseat pattern u<card_number>-<connector>
# Examples: u0-vga-1, u0-hdmi-a-1, u1-dp-2
# Must have UID >= 1000 and match the regex to safeguard real user accounts (e.g. senay)
PHANTOM_USERS=$(awk -F: '$3 >= 1000 && $1 ~ /^u[0-9]+-[a-zA-Z0-9_-]+$/ {print $1}' /etc/passwd)

if [ -n "$PHANTOM_USERS" ]; then
    for pu in $PHANTOM_USERS; do
        log_info "Found auto-generated seat user: $pu"
        
        # Kill any processes owned by this user
        pkill -u "$pu" -9 2>/dev/null || true
        
        # Remove user and home directory
        userdel -r "$pu" 2>/dev/null || userdel -f "$pu" 2>/dev/null || true
        
        # Clean up temporary directories and AccountsService entries
        rm -rf "/tmp/$pu" "/home/$pu" 2>/dev/null || true
        rm -f "/var/lib/AccountsService/users/$pu" 2>/dev/null || true
        
        log_ok "Removed user '$pu' and purged its profile."
    done
else
    log_info "No auto-generated seat users found in /etc/passwd."
fi

# ------------------------------------------------------------------------------
# 7. Clean Multiseat Configuration & Runtime Directories
# ------------------------------------------------------------------------------
log_section "Removing Multiseat Configs & Sockets"

# Config directory
if [ -d /etc/multiseat ]; then
    log_info "Removing /etc/multiseat..."
    rm -rf /etc/multiseat
fi

# Runtime sockets and temporary files
log_info "Removing runtime sockets and temporary configuration files..."
rm -rf /var/local/run/drm-lease-manager
rm -f /tmp/multiseat.conf /tmp/multiseat_cfg.tmp /tmp/usb_user_fail.log

# GUI desktop launcher and binary
rm -f /usr/share/applications/multiseat-gui.desktop
rm -f /usr/local/bin/multiseat-gui

# Clean library symlinks placed in /usr/ from /usr/local/
log_info "Removing library symlinks pointing to /usr/local/..."
for symlink in \
    /usr/include/libdlmclient/dlmclient.h \
    /usr/include/toml.h \
    /usr/lib/pkgconfig/libdlmclient.pc \
    /usr/lib/pkgconfig/libtoml.pc \
    /usr/lib/libdlmclient.so.0 \
    /usr/lib/libtoml.so \
    /usr/lib/libwlroots-0.20.so \
    /usr/lib/libwlroots-0.18.so
do
    if [ -L "$symlink" ]; then
        rm -f "$symlink"
    fi
done
rmdir /usr/include/libdlmclient 2>/dev/null || true

# Refresh ldconfig cache
if command -v ldconfig >/dev/null 2>&1; then
    ldconfig /usr/local/lib 2>/dev/null || true
    ldconfig 2>/dev/null || true
fi
log_ok "Configuration files, sockets, and symlinks removed."

# ------------------------------------------------------------------------------
# 8. Optional Purge of Build Directory & Binaries
# ------------------------------------------------------------------------------
if [ "$PURGE" = true ]; then
    log_section "Purging Source Tree & Compiled Binaries (--purge)"
    
    log_info "Removing /home/multiseat..."
    rm -rf /home/multiseat

    log_info "Removing binaries in /usr/local/bin..."
    rm -f /usr/local/bin/drm-lease-manager \
          /usr/local/bin/labwc \
          /usr/local/bin/sfwbar \
          /usr/local/bin/wlopm

    log_info "Removing libraries in /usr/local/lib..."
    rm -rf /usr/local/include/libdlmclient \
           /usr/local/include/toml.h \
           /usr/local/lib/libdlmclient* \
           /usr/local/lib/libtoml* \
           /usr/local/lib/libwlroots* \
           /usr/local/lib/pkgconfig/libdlmclient.pc \
           /usr/local/lib/pkgconfig/libtoml.pc

    ldconfig 2>/dev/null || true
    log_ok "Purge complete."
fi

# ------------------------------------------------------------------------------
# 9. Installation to PATH for persistent accessibility
# ------------------------------------------------------------------------------
log_section "Ensuring Uninstaller Accessibility"
TARGET_BIN="/usr/local/bin/multiseat-uninstall"
TARGET_SBIN="/usr/local/sbin/multiseat-uninstall"
CURRENT_SCRIPT="$(readlink -f "$0")"

mkdir -p /usr/local/bin /usr/local/sbin
if [ "$CURRENT_SCRIPT" != "$TARGET_BIN" ]; then
    cp -f "$CURRENT_SCRIPT" "$TARGET_BIN"
    chmod 755 "$TARGET_BIN"
    ln -sf "$TARGET_BIN" "$TARGET_SBIN"
    log_ok "Uninstaller installed to $TARGET_BIN and $TARGET_SBIN"
fi

# ------------------------------------------------------------------------------
# 10. Summary & Next Steps
# ------------------------------------------------------------------------------
log_section "Cleanup Completed Successfully"
echo -e "${GREEN}All multiseat configurations have been completely destroyed.${RESET}"
echo -e "System display settings reverted to default: ${BOLD}graphical.target (LightDM / seat0)${RESET}."
echo ""
echo "Next actions:"
echo "  1. If you are in recovery mode: you can reboot now by typing 'reboot'."
echo "  2. If you are in a live terminal: you can restart LightDM with:"
echo "       sudo systemctl restart lightdm"
echo ""

if [ "$REBOOT_PROMPT" = true ]; then
    log_warn "Rebooting system now..."
    reboot
fi
