from __future__ import annotations

import base64
import html
from pathlib import Path

from .data import PROJECT_ROOT
from .roles import ReviewRole


def role_voice_card_html(role: ReviewRole, text: str) -> str:
    avatar_path = PROJECT_ROOT / role.avatar_path
    avatar = avatar_path.read_text(encoding="utf-8") if avatar_path.is_file() else ""
    text_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    persona_name = html.escape(role.persona_name)
    role_name = html.escape(role.name)
    rate = max(0.75, min(1.25, role.voice_rate))
    pitch = max(0.8, min(1.3, role.voice_pitch))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: Arial, sans-serif; color: #23313c; }}
  .card {{ display:flex; align-items:center; gap:14px; min-height:94px; padding:8px 12px; border:1px solid #d8e3e5; border-radius:14px; background:#fbfdfc; }}
  .avatar {{ width:74px; height:74px; flex:0 0 74px; border-radius:50%; overflow:hidden; box-shadow:0 2px 8px #15333b22; }}
  .avatar svg {{ width:100%; height:100%; display:block; }}
  .avatar.talking #mouth {{ transform-box:fill-box; transform-origin:center; animation:talk 170ms ease-in-out infinite alternate; }}
  @keyframes talk {{ from {{ transform:scaleY(.6); }} to {{ transform:scaleY(1.8); }} }}
  .identity {{ flex:1; min-width:0; }}
  .name {{ font-size:16px; font-weight:700; }}
  .role {{ color:#61727a; font-size:12px; margin-top:4px; }}
  .controls {{ display:flex; gap:8px; align-items:center; margin-top:8px; }}
  button {{ border:0; border-radius:8px; padding:7px 12px; color:white; background:#176b67; font-size:12px; cursor:pointer; }}
  button:hover {{ background:#105652; }}
  .status {{ color:#6a7b81; font-size:11px; }}
</style></head><body>
<div class="card">
  <div class="avatar" id="avatar">{avatar}</div>
  <div class="identity">
    <div class="name">{persona_name}</div>
    <div class="role">{role_name}</div>
    <div class="controls"><button id="speak">朗读角色回复</button><span class="status" id="status">本地语音</span></div>
  </div>
</div>
<script>
(() => {{
  const bytes = Uint8Array.from(atob("{text_b64}"), c => c.charCodeAt(0));
  const speechText = new TextDecoder().decode(bytes);
  const button = document.getElementById("speak");
  const avatar = document.getElementById("avatar");
  const status = document.getElementById("status");
  const rate = {rate};
  const pitch = {pitch};
  function chooseVoice(lang) {{
    const voices = window.speechSynthesis.getVoices();
    return voices.find(v => v.lang.toLowerCase().startsWith(lang.toLowerCase())) || voices[0] || null;
  }}
  button.addEventListener("click", () => {{
    if (!("speechSynthesis" in window) || !("SpeechSynthesisUtterance" in window)) {{
      status.textContent = "当前浏览器未提供语音合成";
      return;
    }}
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(speechText);
    utterance.lang = /[\\u4e00-\\u9fff]/.test(speechText) ? "zh-CN" : "en-US";
    utterance.rate = rate;
    utterance.pitch = pitch;
    const voice = chooseVoice(utterance.lang);
    if (voice) utterance.voice = voice;
    utterance.onstart = () => {{ avatar.classList.add("talking"); status.textContent = "正在朗读"; }};
    utterance.onend = () => {{ avatar.classList.remove("talking"); status.textContent = "本地语音"; }};
    utterance.onerror = () => {{ avatar.classList.remove("talking"); status.textContent = "语音播放失败"; }};
    window.speechSynthesis.speak(utterance);
  }});
}})();
</script></body></html>"""

