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
