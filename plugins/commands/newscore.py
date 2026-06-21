"""
plugins/commands/newscore.py
────────────────────────────
Sistem Skor Keaktifan & Admin Otomatis (NewsCore).

Fitur:
  • Track setiap pesan member (non-admin) → tambah skor di MongoDB
  • Background worker → cek waktu reset, angkat admin otomatis
  • /ns_score  — lihat leaderboard grup (admin only)
  • /ns_reset  — paksa reset sekarang (owner only, dev/test)
"""

import asyncio
from datetime import datetime
from html import escape as _html_escape

from pyrogram import Client, filters
from pyrogram.types import Message, ChatPrivileges
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait

from database import (
    ns_get_config, ns_update, ns_calc_next_reset,
    ns_track_message, ns_get_leaderboard, ns_reset_scores,
    ns_get_current_admins, ns_set_current_admins,
    HARI_MAP_NS, is_admin, TZ_WIB,
)
from plugins.ui.handlers_fsm import _truncate_to_utf16_limit
from core.member_tag import set_chat_member_tag

import os
_OWNER_ID = int(os.environ.get("OWNER_ID", 0))


# ─────────────────────────────────────────────────────────────────────────────
#  TRACK PESAN MEMBER (non-admin only)
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.group & ~filters.service & ~filters.bot, group=15)
async def ns_track(client, message: Message):
    """
    Hitung skor hanya jika:
    - Pengirim bukan bot
    - Pengirim bukan admin/owner grup, KECUALI admin yang diangkat oleh
      bot ini melalui NewsCore periode sebelumnya (NS admin aktif)
    - Pesan bukan command
    - Pesan TIDAK dihapus oleh worker spam (antispam/bio/cas)
    """
    try:
        if not message.from_user or message.from_user.is_bot:
            return
        if message.text and message.text.startswith("/"):
            return

        chat_id = message.chat.id
        user_id = message.from_user.id

        cfg = await ns_get_config(chat_id)
        if not cfg.get("enabled"):
            return

        # Cek apakah user adalah admin di grup
        if await is_admin(client, chat_id, user_id):
            # Izinkan hanya jika dia adalah NS admin (diangkat bot via NewsCore)
            # Admin lain (manual/owner) tetap di-skip
            ns_admins = await ns_get_current_admins(chat_id)
            ns_admin_ids = {a["user_id"] for a in ns_admins}
            if user_id not in ns_admin_ids:
                return

        # Beri jeda kecil agar antispam/bio/cas sempat mark_message_handled
        await asyncio.sleep(0.35)

        # Jika sudah di-mark oleh worker penghapus → skip, tidak dihitung
        from database import is_message_handled
        if is_message_handled(chat_id, message.id):
            return

        await ns_track_message(
            chat_id=chat_id,
            user_id=user_id,
            user_name=message.from_user.first_name or "User",
        )
    except Exception as e:
        print(f"[NewsCore] track handler error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  LEADERBOARD COMMAND  /ns_score
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("ns_score") & filters.group, group=20)
async def cmd_ns_score(client, message: Message):
    try:
        chat_id = message.chat.id
        uid     = message.from_user.id if message.from_user else 0
        if not await is_admin(client, chat_id, uid):
            return

        cfg = await ns_get_config(chat_id)
        if not cfg.get("enabled"):
            rep = await message.reply_text(
                "⚠️ <b>NewsCore</b> belum diaktifkan di grup ini.\n"
                "Aktifkan via <b>⚙️ Kelola Grup → 🏆 NewsCore</b>.",
                parse_mode=ParseMode.HTML,
            )
            asyncio.create_task(_auto_del([message, rep], 10))
            return

        top = await ns_get_leaderboard(chat_id, 10)
        if not top:
            rep = await message.reply_text(
                "📭 Belum ada data keaktifan periode ini.",
                parse_mode=ParseMode.HTML,
            )
            asyncio.create_task(_auto_del([message, rep], 10))
            return

        lines = "".join(
            f"{i}. <b>{m['user_name']}</b> — <code>{m['score']}</code> poin\n"
            for i, m in enumerate(top, 1)
        )

        next_r = cfg.get("next_reset")
        next_str = ""
        if next_r:
            try:
                next_str = f"\n📅 Reset berikutnya: <code>{datetime.fromisoformat(next_r).strftime('%d %b %Y %H:%M')}</code> WIB"
            except Exception:
                pass

        rep = await message.reply_text(
            f"🏆 <b>PAPAN SKOR KEAKTIFAN</b>\n"
            f"<code>Grup: {chat_id}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{lines}"
            f"{next_str}",
            parse_mode=ParseMode.HTML,
        )
        asyncio.create_task(_auto_del([message, rep], 30))
    except Exception as e:
        print(f"[NewsCore] /ns_score error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  FORCE RESET COMMAND  /ns_reset  (owner only)
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("ns_reset") & filters.group, group=20)
async def cmd_ns_reset(client, message: Message):
    try:
        uid = message.from_user.id if message.from_user else 0
        if uid != _OWNER_ID:
            return
        await message.reply_text("⏳ Memulai simulasi reset NewsCore…")
        await ns_do_reset(client, message.chat.id)
    except Exception as e:
        print(f"[NewsCore] /ns_reset error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  CORE RESET JOB
# ─────────────────────────────────────────────────────────────────────────────

async def _apply_auto_title_member(client, chat_id: int, cfg: dict, admin_ids: set) -> str:
    """
    Pasang tag otomatis ke member NON-admin berdasar rank leaderboard typing
    NewsCore, sesuai kelompok 5-rank per nama yang diisi owner (maks 10 nama
    -> cover rank 1-50). Dipanggil dari ns_do_reset(), terpisah dari logika
    pengangkatan admin di atas (admin_ids = user yang baru diangkat admin
    periode ini, di-exclude dari pemberian tag karena admin pakai mekanisme
    titel admin/custom_title, bukan tag member).

    Returns ringkasan singkat (string) untuk disisipkan ke pengumuman reset,
    atau "" jika fitur tidak aktif / tidak ada nama diisi / tidak ada member
    yang memenuhi syarat.
    """
    if not cfg.get("auto_title_enabled", False):
        return ""

    names = [n for n in cfg.get("auto_title_names", []) if n and n.strip()]
    if not names:
        return ""

    # Butuh leaderboard sampai cover seluruh kelompok nama yang diisi
    # (maks 10 nama x 5 rank = 50), supaya rank terakhir tetap dapat tag
    # walau owner mengisi semua 10 slot.
    pool_size  = len(names) * 5
    full_board = await ns_get_leaderboard(chat_id, pool_size + len(admin_ids))

    # Saring member yang baru jadi admin periode ini — mereka dapat titel
    # admin (custom_title), BUKAN tag member, supaya tidak tertimpa/konflik.
    candidates = [w for w in full_board if w["user_id"] not in admin_ids][:pool_size]

    if not candidates:
        return ""

    ok_count, fail_count = 0, 0
    fail_samples = []

    for idx, w in enumerate(candidates):
        group_idx = idx // 5  # 0 = rank 1-5, 1 = rank 6-10, dst
        if group_idx >= len(names):
            break
        tag = _truncate_to_utf16_limit(names[group_idx], 16)
        uid = w["user_id"]

        success, reason = await set_chat_member_tag(chat_id, uid, tag)
        if success:
            ok_count += 1
        else:
            fail_count += 1
            if len(fail_samples) < 3:
                fail_samples.append(f"{w.get('user_name', uid)}: {reason}")
            print(f"[NewsCore][AutoTitle] gagal uid={uid} tag={tag!r}: {reason}")

    if ok_count == 0 and fail_count == 0:
        return ""

    summary = f"\n\n🏷️ <b>Auto Title Member:</b> <code>{ok_count}</code> member ditandai otomatis."
    if fail_count:
        summary += (
            f"\n⚠️ <code>{fail_count}</code> gagal — kemungkinan bot belum "
            f"punya hak <code>can_manage_tags</code>."
        )
    return summary


async def ns_do_reset(client, chat_id: int):
    """Angkat admin berdasarkan skor tertinggi, lalu reset semua skor."""
    try:
        # Ambil config terbaru dari DB (bukan cache lama)
        cfg         = await ns_get_config(chat_id)
        max_admins  = cfg.get("max_admins", 1)
        p           = cfg.get("privileges", {})
        admin_title = (cfg.get("admin_title") or "").strip()

        top = await ns_get_leaderboard(chat_id, max_admins)

        # Copot admin lama yang tidak masuk top baru
        old_admins = await ns_get_current_admins(chat_id)
        new_ids    = {m["user_id"] for m in top}
        for old in old_admins:
            if old["user_id"] not in new_ids:
                try:
                    await client.promote_chat_member(
                        chat_id=chat_id, user_id=old["user_id"],
                        privileges=ChatPrivileges(can_manage_chat=False),
                    )
                except Exception:
                    pass

        ann = "📢 <b>PERGANTIAN ADMIN NEWSCORE PERIODE BARU!</b> 📢\n\n"
        new_admin_docs = []

        if top:
            ann += f"🏆 <b>Top {len(top)} member teraktif:</b>\n\n"
            for idx, w in enumerate(top, 1):
                uid   = w["user_id"]
                uname = w["user_name"]
                # Retry sekali jika kena FloodWait, agar promosi benar-benar
                # tereksekusi alih-alih di-skip diam-diam setelah sleep.
                for _attempt in range(2):
                    try:
                        await client.promote_chat_member(
                            chat_id=chat_id, user_id=uid,
                            privileges=ChatPrivileges(
                                can_manage_chat=True,
                                can_delete_messages=p.get("can_delete_messages", True),
                                can_restrict_members=p.get("can_restrict_members", True),
                                can_invite_users=p.get("can_invite_users", True),
                                can_pin_messages=p.get("can_pin_messages", True),
                                can_manage_video_chats=p.get("can_manage_video_chats", False),
                            ),
                        )
                        title_ok = False
                        title    = admin_title if admin_title else f"Top Member {idx} 👑"
                        # Telegram membatasi custom title admin maks 16 UTF-16
                        # code unit (bukan 16 codepoint Python) — pakai helper
                        # yang aman terhadap font unik/combining mark agar
                        # tidak memotong di tengah grapheme cluster.
                        title = _truncate_to_utf16_limit(title, 16)
                        # Retry singkat: tepat setelah promote_chat_member,
                        # Telegram kadang belum selesai mencatat status admin
                        # baru di sisi server → set_administrator_title
                        # bisa gagal sesaat (race condition). 3x coba dengan
                        # delay kecil sebelum benar-benar menyerah.
                        for _title_attempt in range(3):
                            try:
                                await client.set_administrator_title(
                                    chat_id, uid, title
                                )
                                title_ok = True
                                break
                            except FloodWait as fw_title:
                                await asyncio.sleep(fw_title.value)
                                continue
                            except Exception as e_title:
                                print(f"[NewsCore] set_custom_title gagal uid={uid} attempt={_title_attempt+1}: {e_title}")
                                await asyncio.sleep(1.0)
                                continue
                        if not title_ok:
                            print(f"[NewsCore] set_custom_title MENYERAH uid={uid} title={title!r}")
                        new_admin_docs.append({"chat_id": chat_id, "user_id": uid, "user_name": uname})
                        title_note = "" if title_ok else " (⚠️ titel gagal dipasang)"
                        ann += f"{idx}. <a href='tg://user?id={uid}'>{uname}</a> — <code>{w['score']}</code> poin{title_note}\n"
                        break
                    except FloodWait as fw:
                        await asyncio.sleep(fw.value)
                        continue
                    except Exception as e:
                        print(f"[NewsCore] promote error uid={uid}: {e}")
                        ann += f"{idx}. <b>{uname}</b> (⚠️ gagal dipromosikan)\n"
                        break
                else:
                    # Kedua percobaan kena FloodWait — laporkan sebagai gagal
                    # agar tidak hilang diam-diam dari pengumuman.
                    print(f"[NewsCore] promote uid={uid} gagal setelah retry FloodWait")
                    ann += f"{idx}. <b>{uname}</b> (⚠️ gagal dipromosikan — FloodWait)\n"
        else:
            ann += "Tidak ada aktivitas periode ini. Posisi admin tetap. 🏝️"

        # Sertakan syarat bio admin wajib di pengumuman, supaya admin baru
        # langsung tahu konsekuensinya (di-unadmin otomatis oleh
        # enforce_admin_bio() di ns_bio_guard.py jika tidak dipenuhi).
        if top:
            bio_admin_text     = (cfg.get("bio_admin_text") or "").strip()
            bio_admin_required = cfg.get("bio_admin_required", True)
            if bio_admin_required and bio_admin_text:
                ann += (
                    f"\n\n📝 <b>Wajib!</b> Admin di atas harus mencantumkan "
                    f"teks berikut di bio Telegram:\n"
                    f"<code>{_html_escape(bio_admin_text)}</code>\n"
                    f"<i>Bio tidak sesuai → otomatis di-unadmin.</i>"
                )
            elif bio_admin_required and not bio_admin_text:
                ann += (
                    f"\n\n⚠️ <b>Perhatian:</b> Syarat bio admin wajib aktif "
                    f"tapi teksnya belum diatur owner — admin di atas berisiko "
                    f"di-unadmin otomatis sampai diatur."
                )

        # Auto Title Member: tag otomatis untuk member non-admin berdasar
        # rank, terpisah dari pengangkatan admin di atas. new_ids = id yang
        # baru diangkat admin periode ini (di-exclude dari pemberian tag).
        auto_title_summary = await _apply_auto_title_member(client, chat_id, cfg, new_ids)
        ann += auto_title_summary

        await ns_set_current_admins(chat_id, new_admin_docs)

        # Hitung next_reset dari config terbaru (bukan cfg lama)
        cfg_fresh = await ns_get_config(chat_id)
        new_next  = ns_calc_next_reset(cfg_fresh)
        await ns_update(chat_id, {"next_reset": new_next})

        ann += (
            f"\n\n🔄 <i>Poin direset ke 0!</i>\n"
            f"📅 Reset berikutnya: <code>{datetime.fromisoformat(new_next).strftime('%d %b %Y %H:%M')}</code> WIB"
        )

        try:
            await client.send_message(chat_id=chat_id, text=ann, parse_mode=ParseMode.HTML)
        except Exception as e:
            print(f"[NewsCore] send announcement error: {e}")

        # Reset skor SETELAH pengumuman dikirim
        await ns_reset_scores(chat_id)

    except Exception as e:
        print(f"[NewsCore] ns_do_reset error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  BACKGROUND TIME-CHECKER LOOP
# ─────────────────────────────────────────────────────────────────────────────

_checker_running = False


async def newscore_checker_loop(client):
    global _checker_running
    if _checker_running:
        return
    _checker_running = True
    print("[NewsCore] Time-checker loop started.")
    while True:
        try:
            from database import newscore_cfg_db
            all_cfgs = await newscore_cfg_db.find({"enabled": True}).to_list(length=200)
            # now WIB-aware, agar konsisten dengan next_reset yang dihitung
            # via ns_calc_next_reset() (juga pakai TZ_WIB) — jika tidak,
            # jam reset bisa meleset sebanyak selisih timezone server vs WIB.
            now = datetime.now(TZ_WIB)
            for cfg in all_cfgs:
                cid      = cfg.get("chat_id")
                next_str = cfg.get("next_reset")
                if cid and next_str:
                    try:
                        target = datetime.fromisoformat(next_str)
                        if target.tzinfo is None:
                            # Data lama yang masih naive (tersimpan sebelum
                            # next_reset memakai TZ_WIB) — anggap sebagai WIB
                            # agar tetap bisa dibandingkan tanpa TypeError.
                            target = target.replace(tzinfo=TZ_WIB)
                        if now >= target:
                            print(f"[NewsCore] Waktunya reset untuk grup {cid}")
                            await ns_do_reset(client, cid)
                    except Exception as e:
                        print(f"[NewsCore] checker reset error cid={cid}: {e}")
        except Exception as e:
            print(f"[NewsCore] checker error: {e}")
        await asyncio.sleep(30)


# ─────────────────────────────────────────────────────────────────────────────
#  Helper
# ─────────────────────────────────────────────────────────────────────────────

async def _auto_del(msgs: list, delay: int):
    await asyncio.sleep(delay)
    for m in msgs:
        try:
            await m.delete()
        except Exception:
            pass

