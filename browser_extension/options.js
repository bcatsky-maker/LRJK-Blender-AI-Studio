const tokenInput = document.getElementById("token");
const status = document.getElementById("status");

chrome.storage.local.get(["lrjkBridgeToken"], (stored) => {
  tokenInput.value = stored.lrjkBridgeToken || "";
});

document.getElementById("save").addEventListener("click", () => {
  const token = tokenInput.value.trim();
  chrome.storage.local.set({ lrjkBridgeToken: token }, () => {
    status.textContent = "Saved!";
    setTimeout(() => { status.textContent = ""; }, 1500);
  });
});
