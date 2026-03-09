#!/bin/bash
# Chief of Staff - Daily Brief
# Usage: cos (via alias)

COS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$COS_DIR"
source .venv/bin/activate

NOTE=$(python renderer.py --stdout 2>/dev/null)

if [ -z "$NOTE" ]; then
    echo "Daily note bos. Once pipeline calistir: ./run.sh"
    exit 1
fi

cat <<EOF | claude -p -
# Daily Brief

Asagidaki daily note icerigini Turkce ozetle.

$NOTE

---

Format:
1. **Takvim**: Toplantilari saatleriyle listele, prep gereken toplantilari belirt
2. **Kritik**: P1/P2 veya acil dikkat gereken konular
3. **Yapilacaklar**: dispatch/prep/yours sayilari, onemlileri vurgula
4. **Bekleyenler**: Carried-over sayisi, dikkat ceken patern varsa belirt

15 satir max. Laf yok, direkt ozet.
EOF
