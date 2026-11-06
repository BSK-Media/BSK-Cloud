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

  form.addEventListener("submit", (e) => {
    if (!window.XMLHttpRequest || !input.files.length) return;
    e.preventDefault();
    const xhr = new XMLHttpRequest();
    xhr.open("POST", form.action);
    xhr.setRequestHeader("X-Requested-With", "fetch");
    bar.hidden = false;
    button.disabled = true;
    xhr.upload.onprogress = (p) => {
      if (p.lengthComputable) {
        bar.value = Math.round((p.loaded / p.total) * 100);
        button.textContent = `Wysyłanie… ${bar.value}%`;
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 400) {
        window.location.href = xhr.responseURL || window.location.href;
      } else {
        let msg = "Wysyłka nie powiodła się.";
        try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (_) {}
        alert(msg);
        button.disabled = false;
        button.textContent = "Wyślij";
        bar.hidden = true;
      }
    };
    xhr.onerror = () => {
      alert("Błąd połączenia — sprawdź internet i spróbuj ponownie.");
      button.disabled = false;
      button.textContent = "Wyślij";
    };
    xhr.send(new FormData(form));
  });
});
