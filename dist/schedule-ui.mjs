import {loadSchedule, replaceSchedule, editorURL} from './schedule.mjs?v=20260905-time';

export function mountScheduleEditor(container, onCurrentTime = () => {}) {
  container.innerHTML = `
    <section class="schedule-editor" aria-labelledby="schedule-title">
      <div class="criteria-heading"><h3 id="schedule-title">每日更新時間</h3><span>香港時間</span></div>
      <p id="schedule-current" role="status">正在核對 GitHub 已儲存時間…</p>
      <div class="schedule-controls"><label for="daily-time">選擇新時間<input type="time" id="daily-time" required disabled></label><button class="button" type="button" id="schedule-prepare" disabled>複製新設定</button><button class="button button-outline" type="button" id="schedule-check">檢查是否已儲存</button></div>
      <p class="field-help">你可以喺此選時間；共同排程由你嘅 GitHub 帳戶確認儲存，瀏覽網站毋須登入。實際執行可能稍為延遲。</p>
      <p id="schedule-message" class="field-help" role="status"></p>
      <div id="schedule-next" class="schedule-next" hidden><p><strong>新時間尚未儲存。</strong> 按下方連結，在 GitHub 編輯框內按 Ctrl+A 全選、Ctrl+V 貼上，再按「Commit changes」確認。之後返呢頁檢查。</p><a class="button" id="schedule-github" target="_blank" rel="noopener noreferrer">開啟 GitHub 確認儲存 ↗</a><details id="schedule-copy-help"><summary>手動複製設定</summary><label class="field-label" for="schedule-text">全選以下設定並複製</label><textarea id="schedule-text" rows="8" readonly spellcheck="false"></textarea></details></div>
    </section>`;
  const find = selector => container.querySelector(selector);
  find('#schedule-github').href = editorURL;
  let requestedTime = null, initialized = false;
  const message = text => { find('#schedule-message').textContent = text; };
  function showCurrent(current) {
    find('#schedule-current').textContent = `GitHub 已儲存：每日 ${current.time}（香港時間）`;
    onCurrentTime(current.time);
    if (!initialized) { find('#daily-time').value = current.time; initialized = true; }
    find('#daily-time').disabled = false;
    find('#schedule-prepare').disabled = false;
  }
  async function check() {
    const button = find('#schedule-check'); button.disabled = true;
    try {
      const current = await loadSchedule();
      if (!container.isConnected) return;
      showCurrent(current);
      if (requestedTime) {
        if (current.time === requestedTime) {
          message(`已確認 GitHub 儲存每日 ${current.time}。之後會按新時間排程。`);
          find('#schedule-next').hidden = true; requestedTime = null;
        } else message(`GitHub 仍然係 ${current.time}，未確認新時間。請先完成 GitHub 儲存；快取可能需要幾分鐘更新。`);
      } else message('以上係 GitHub 目前設定。單純修改選單唔會更改雲端排程。');
    } catch { message('暫時未能核對 GitHub 排程，請稍後再檢查。未有改動任何設定。'); }
    finally { if (container.isConnected) button.disabled = false; }
  }
  find('#daily-time').addEventListener('input',()=>{
    requestedTime = null; find('#schedule-next').hidden = true;
    message('新選擇尚未儲存。複製新設定後，到 GitHub 確認。');
  });
  find('#schedule-prepare').addEventListener('click',async()=>{
    const input=find('#daily-time'), button=find('#schedule-prepare');
    if (!input.reportValidity()) return;
    const target=input.value; button.disabled=true; input.disabled=true;
    try {
      const current=await loadSchedule();
      const updated=replaceSchedule(current.text,target);
      if (!container.isConnected) return;
      showCurrent(current);
      if (current.time===target) { message('GitHub 已經係呢個時間，毋須更改。'); return; }
      button.disabled=true; input.disabled=true;
      requestedTime=target;
      find('#schedule-text').value=updated;
      find('#schedule-next').hidden=false;
      try {
        await navigator.clipboard.writeText(updated);
        message(`已複製每日 ${target} 嘅設定；請到 GitHub 確認儲存。`);
      } catch {
        find('#schedule-copy-help').open=true;
        find('#schedule-text').focus(); find('#schedule-text').select();
        message('瀏覽器未能自動複製。請按 Ctrl+C 複製已選取設定，再到 GitHub 儲存。');
      }
    } catch { message('未能準備最新排程，請稍後重試。未有改動任何設定。'); }
    finally { if (container.isConnected) { button.disabled=false; input.disabled=false; } }
  });
  find('#schedule-check').addEventListener('click',check);
  check();
}
