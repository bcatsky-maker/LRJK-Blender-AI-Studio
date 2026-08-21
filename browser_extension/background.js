// Register Context Menu on Install
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "sendModelToLRJK",
    title: "📥 Send Model to LRJK AI Studio",
    contexts: ["selection", "link", "page"]
  });
});

// Handle Context Menu Item Click
chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "sendModelToLRJK") {
    // Grab selected text, link URL, or fallback to current tab URL
    let payloadData = info.linkUrl || info.selectionText || tab.url;

    if (!payloadData) return;

    // The desktop app's bridge server now requires a pairing token (see
    // src/ui/main_window.py's BridgeHTTPRequestHandler) on every POST, to
    // stop any other local process - or a "simple" cross-origin request
    // from an unrelated webpage - from silently talking to it. Paste the
    // token shown in the desktop app's AI Settings dialog into this
    // extension's options page (click the toolbar icon) once; it's kept
    // in chrome.storage.local from then on.
    chrome.storage.local.get(["lrjkBridgeToken"], (stored) => {
      const token = stored.lrjkBridgeToken || "";
      if (!token) {
        console.warn("⚠️ LRJK AI Studio: no Bridge Token configured. Click the extension icon to set it.");
      }

      fetch("http://127.0.0.1:8081/", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-LRJK-Token": token
        },
        body: JSON.stringify({
          type: "browser_model_import",
          prompt: payloadData.trim(),
          source_url: tab.url
        })
      })
        .then(response => response.json())
        .then(data => {
          console.log("✅ Model successfully sent to LRJK Studio:", data);
        })
        .catch(error => {
          console.warn("⚠️ Could not reach LRJK Studio bridge server (Is the app running? Is the token set?):", error);
        });
    });
  }
});
