// Uruchamia edytor ONLYOFFICE z konfiguracją podpisaną przez serwer (bez skryptów inline — CSP).
(function () {
  const el = document.getElementById("office-editor");
  if (!el) return;
  if (!window.DocsAPI) {
    el.outerHTML = '<div class="empty">Nie udało się połączyć z serwerem dokumentów. Spróbuj odświeżyć stronę.</div>';
    return;
  }
  const config = JSON.parse(el.dataset.config);
  new window.DocsAPI.DocEditor("office-editor", config);
})();
