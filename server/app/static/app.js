// Potwierdzenia, przeciąganie plików i pasek postępu wysyłki (bez skryptów inline — CSP).
document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (e) => {
    if (!confirm(form.dataset.confirm)) e.preventDefault();
  });
});

document.querySelectorAll("form[data-upload]").forEach((form) => {
  const drop = form.querySelector(".drop");
  const input = form.querySelector("input[type=file]");
  const label = drop.querySelector("span");
  const bar = form.querySelector("progress");
  const button = form.querySelector("button");
  const showNames = () => {
    const n = input.files.length;
    if (n) label.textContent = n === 1 ? input.files[0].name : `Wybrano plików: ${n}`;
  };
  input.addEventListener("change", showNames);
  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    input.files = e.dataTransfer.files;
    showNames();
  });

  form.addEventListener("submit", async (e) => {
    if (!window.fetch || !window.Blob || !input.files.length) return;
    e.preventDefault();
    const files = Array.from(input.files);
    const csrf = form.querySelector("input[name=csrf]").value;
    const total = files.reduce((n, f) => n + f.size, 0) || 1;
    let done = 0;
    bar.hidden = false;
    button.disabled = true;
    const show = (i, extra) => {
      bar.value = Math.floor((done / total) * 100);
      button.textContent = `Wysyłanie ${i + 1}/${files.length}… ${bar.value}%${extra || ""}`;
    };
    try {
      const ids = [];
      for (let i = 0; i < files.length; i++) {
        ids.push(await sendChunked(files[i], csrf, (sent) => { done += sent; show(i); },
                                   (msg) => show(i, msg)));
      }
      const data = new FormData(form);
      data.delete("files");
      ids.forEach((id) => data.append("upload_ids", id));
      const res = await fetch(form.action, {
        method: "POST", body: data, credentials: "same-origin", headers: { "X-Requested-With": "fetch" },
      });
      if (!res.ok) throw new Error(await errorText(res));
      const next = await res.json().catch(() => ({}));
      window.location.href = next.redirect || window.location.href;
    } catch (err) {
      alert(err.message || "Wysyłka nie powiodła się.");
      button.disabled = false;
      button.textContent = "Wyślij";
      bar.hidden = true;
    }
  });
});

async function errorText(res) {
  try { return (await res.json()).detail || `Błąd ${res.status}`; } catch (_) { return `Błąd ${res.status}`; }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Wysyła plik w kawałkach (~50 MB). Po zerwaniu połączenia ponawia i wznawia od miejsca przerwania.
async function sendChunked(file, csrf, onProgress, onStatus) {
  const headers = { "X-CSRF-Token": csrf, "Content-Type": "application/json" };
  const init = await fetch("/api/uploads", {
    method: "POST", credentials: "same-origin", headers, body: JSON.stringify({ name: file.name, size: file.size }),
  });
  if (!init.ok) throw new Error(await errorText(init));
  const up = await init.json();
  let offset = up.received || 0;
  let failures = 0;
  while (offset < file.size) {
    const end = Math.min(offset + up.chunk_size, file.size);
    try {
      const res = await fetch(`/api/uploads/${up.id}?offset=${offset}`, {
        method: "PUT", credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf, "Content-Type": "application/octet-stream" },
        body: file.slice(offset, end),
      });
      if (res.status === 409) {
        const info = await res.json();
        if (typeof info.received === "number") { onProgress(info.received - offset); offset = info.received; continue; }
      }
      if (!res.ok) {
        if (res.status >= 500 || res.status === 429) throw new TypeError("server");
        throw new Error(await errorText(res));
      }
      const info = await res.json();
      onProgress(info.received - offset);
      offset = info.received;
      failures = 0;
    } catch (err) {
      if (!(err instanceof TypeError)) throw err; // błąd z serwera z komunikatem — nie ponawiamy
      failures += 1;
      if (failures > 30) throw new Error("Brak połączenia z serwerem. Spróbuj ponownie później.");
      const wait = Math.min(30, 2 ** Math.min(failures, 5));
      onStatus(` — brak połączenia, ponawiam za ${wait} s`);
      await sleep(wait * 1000);
      try { // dowiedz się, ile serwer już ma
        const st = await fetch(`/api/uploads/${up.id}`, { credentials: "same-origin", headers: { "X-CSRF-Token": csrf } });
        if (st.ok) { const info = await st.json(); onProgress(info.received - offset); offset = info.received; }
      } catch (_) { /* nadal offline */ }
    }
  }
  return up.id;
}

// Kopiowanie linków do schowka
document.querySelectorAll("[data-copy]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const input = btn.parentElement.querySelector("[data-copy-source]");
    input.select();
    try { await navigator.clipboard.writeText(input.value); } catch (_) { document.execCommand("copy"); }
    btn.textContent = "Skopiowano";
    setTimeout(() => { btn.textContent = "Kopiuj"; }, 2000);
  });
});

// Zakładki filtrujące listę plików (Wszystkie / od klienta / od BSK Media)
document.querySelectorAll("[data-filter-tabs]").forEach((tabs) => {
  const table = tabs.closest(".card").querySelector("table[data-filterable]");
  if (!table) return;
  tabs.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
      table.querySelectorAll("tbody tr").forEach((tr) => {
        tr.hidden = btn.dataset.f !== "all" && tr.dataset.from !== btn.dataset.f;
      });
    });
  });
});

// Wątek wiadomości zaczyna się od najnowszej
document.querySelectorAll("[data-scroll-end]").forEach((el) => { el.scrollTop = el.scrollHeight; });

// Zamknij menu boczne na telefonie po wyborze pozycji
document.querySelectorAll(".sidebar a").forEach((a) => a.addEventListener("click", () => {
  const t = document.getElementById("nav-toggle");
  if (t) t.checked = false;
}));

// ---------------------------------------------------------------- wiadomości na żywo
const baseTitle = document.title;
let audioCtx = null;
document.addEventListener("pointerdown", () => {
  // przeglądarki pozwalają odtwarzać dźwięk dopiero po interakcji użytkownika
  if (!audioCtx && window.AudioContext) audioCtx = new AudioContext();
}, { once: true });

function ding() {
  if (!audioCtx) return;
  const t = audioCtx.currentTime;
  [880, 1320].forEach((f, i) => {
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.frequency.value = f; o.type = "sine";
    g.gain.setValueAtTime(0.0001, t + i * 0.12);
    g.gain.exponentialRampToValueAtTime(0.18, t + i * 0.12 + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.12 + 0.25);
    o.connect(g).connect(audioCtx.destination);
    o.start(t + i * 0.12); o.stop(t + i * 0.12 + 0.3);
  });
}

function desktopNotify(msg) {
  if (!("Notification" in window) || Notification.permission !== "granted" || !document.hidden) return;
  const n = new Notification(`Wiadomość od ${msg.author}`, { body: msg.body.slice(0, 140), tag: "bsk-thread" });
  n.onclick = () => { window.focus(); n.close(); };
}

function setBadges(count) {
  document.querySelectorAll("[data-unread-badge]").forEach((b) => {
    b.textContent = count;
    b.hidden = !count;
  });
  document.title = count ? `(${count}) ${baseTitle}` : baseTitle;
}

function renderMessage(m) {
  const wrap = document.createElement("div");
  wrap.className = "msg fresh" + (m.mine ? " mine" : "");
  const av = document.createElement("span");
  av.className = "avatar" + (m.agency ? " agency" : "");
  av.textContent = m.initials;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = `${m.author}${m.agency ? " · BSK Media" : ""} · ${m.time}`;
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = m.body;
  bubble.append(meta, body);
  wrap.append(av, bubble);
  return wrap;
}

document.querySelectorAll("[data-thread]").forEach((thread) => {
  const clientId = thread.dataset.thread;
  let last = Number(thread.dataset.last || 0);
  let busy = false;

  async function poll() {
    if (busy) return;
    busy = true;
    try {
      const res = await fetch(`/api/thread/${clientId}?after=${last}&visible=${document.hidden ? 0 : 1}`,
                              { credentials: "same-origin" });
      if (!res.ok) return;
      const data = await res.json();
      if (data.messages.length) {
        const nearBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
        const empty = thread.querySelector("[data-empty]");
        if (empty) empty.remove();
        data.messages.forEach((m) => {
          thread.append(renderMessage(m));
          last = Math.max(last, m.id);
          if (!m.mine) { ding(); desktopNotify(m); }
        });
        if (nearBottom || data.messages.some((m) => m.mine)) thread.scrollTop = thread.scrollHeight;
      }
      setBadges(data.unread);
    } catch (_) { /* chwilowy brak sieci — spróbujemy za chwilę */ } finally { busy = false; }
  }
  setInterval(poll, 4000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

  const composer = thread.parentElement.querySelector("[data-live-composer]");
  if (composer && window.fetch) {
    const area = composer.querySelector("textarea");
    area.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) composer.requestSubmit();
    });
    composer.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!area.value.trim()) return;
      const btn = composer.querySelector("button");
      btn.disabled = true;
      try {
        const res = await fetch(composer.action, {
          method: "POST", body: new FormData(composer), credentials: "same-origin",
          headers: { "X-Requested-With": "fetch" },
        });
        if (!res.ok) throw new Error();
        area.value = "";
        await poll();
      } catch (_) {
        alert("Nie udało się wysłać wiadomości — sprawdź połączenie i spróbuj ponownie.");
      } finally { btn.disabled = false; area.focus(); }
    });
  }
});

// Przycisk „Włącz powiadomienia” (powiadomienia systemowe przeglądarki)
document.querySelectorAll("[data-notify-enable]").forEach((btn) => {
  if (!("Notification" in window) || Notification.permission !== "default") return;
  btn.hidden = false;
  btn.addEventListener("click", async () => {
    const p = await Notification.requestPermission();
    btn.hidden = p !== "default";
    if (p === "granted") new Notification("Powiadomienia włączone", { body: "Damy znać o nowych wiadomościach." });
  });
});

// Licznik nieprzeczytanych na pozostałych stronach (co 30 s)
if (document.querySelector("[data-unread-poll]") && !document.querySelector("[data-thread]")) {
  let known = Number(document.querySelector("[data-unread-poll]").dataset.unread || 0);
  setBadges(known);
  setInterval(async () => {
    try {
      const res = await fetch("/api/unread", { credentials: "same-origin" });
      if (!res.ok) return;
      const { unread } = await res.json();
      if (unread > known) ding();
      known = unread;
      setBadges(unread);
    } catch (_) { /* offline */ }
  }, 30000);
}
