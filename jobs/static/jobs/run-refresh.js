const refreshButton = document.getElementById("toggle-run-refresh");
const storageKey = "jobs-run-refresh";

let refreshTimer;

function setRefresh(enabled) {
  refreshButton.setAttribute("aria-pressed", String(enabled));
  clearInterval(refreshTimer);
  if (enabled) {
    refreshTimer = setInterval(async () => {
      if (document.hidden) return;
      try {
        const response = await fetch(location.href, { credentials: "same-origin" });
        if (!response.ok) return;
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        const newRows = page.querySelector("#result_list tbody");
        const currentRows = document.querySelector("#result_list tbody");
        if (currentRows?.contains(document.activeElement)) return;
        if (newRows && currentRows) {
          currentRows.replaceWith(newRows);
        } else if (newRows) {
          const results = page.querySelector("#changelist-form");
          document.querySelector("#changelist-form")?.replaceWith(results);
        }
      } catch {
        // Try again at the next interval.
      }
    }, 5000);
  }
}

refreshButton.hidden = false;
setRefresh(sessionStorage.getItem(storageKey) === "on");
refreshButton.addEventListener("click", () => {
  const enabled = refreshButton.getAttribute("aria-pressed") !== "true";
  sessionStorage.setItem(storageKey, enabled ? "on" : "off");
  setRefresh(enabled);
});
