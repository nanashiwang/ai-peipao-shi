// 前端安全启动脚本：在 app.js 前加载，给旧式手工渲染提供统一兜底。
(function () {
  "use strict";

  const AUTH_STORAGE_KEYS = new Set(["controlUser", "chatUser"]);
  const SAFE_URL_PROTOCOLS = new Set(["http:", "https:", "mailto:", "tel:", "blob:"]);
  const SAFE_INLINE_HANDLERS = new Set([
    "ackParentReport",
    "approveReport",
    "batchAgent",
    "cancelTask",
    "createBackup",
    "createReportTask",
    "createTaskFromOutput",
    "deleteKnowledgeChunk",
    "logoutCurrentUser",
    "manualVerifySendLog",
    "openAccountSettings",
    "openSendArtifact",
    "prepareReply",
    "previewWecomFamily",
    "pruneRetention",
    "queueConversationProof",
    "queueTaskDryRun",
    "queueTaskRealSend",
    "refreshAgentEval",
    "refreshAgentConfig",
    "refreshAll",
    "refreshRetention",
    "refreshServiceQuality",
    "refreshWorkbenchOverview",
    "requestAllConversationProofs",
    "requestConversationProof",
    "requestMissingConversationProofs",
    "retryTask",
    "runAgentForFamily",
    "runBackupDrill",
    "runFamilyAiBundle",
    "saveOutput",
    "saveAgentConfig",
    "saveTask",
    "saveTaskFromChat",
    "saveTemplate",
    "selectChat",
    "sendTask",
    "sendTaskFromChat",
    "setSelectedFamily",
    "showAuth",
    "showLanding",
    "submitParentReportFeedback",
    "switchAuthTab",
    "switchPanelGroup",
    "switchTab",
    "syncWecomArchive",
    "toggleDeviceAnyConversation",
    "toggleDeviceRealSend",
    "toggleTemplate",
  ]);

  const nativeStorage = {
    getItem: Storage.prototype.getItem,
    setItem: Storage.prototype.setItem,
    removeItem: Storage.prototype.removeItem,
  };
  const nativeInnerHTML = Object.getOwnPropertyDescriptor(Element.prototype, "innerHTML");
  const nativeInsertAdjacentHTML = Element.prototype.insertAdjacentHTML;
  const nativeSetAttribute = Element.prototype.setAttribute;

  function storageGet(storage, key) {
    try { return nativeStorage.getItem.call(storage, key); } catch { return null; }
  }

  function storageSet(storage, key, value) {
    try { nativeStorage.setItem.call(storage, key, value); } catch { /* 忽略隐私模式/配额错误 */ }
  }

  function storageRemove(storage, key) {
    try { nativeStorage.removeItem.call(storage, key); } catch { /* 忽略隐私模式/配额错误 */ }
  }

  function migrateAuthValue(key) {
    const current = storageGet(window.sessionStorage, key);
    if (current) return current;
    const legacy = storageGet(window.localStorage, key);
    if (legacy) storageSet(window.sessionStorage, key, legacy);
    storageRemove(window.localStorage, key);
    return legacy;
  }

  Storage.prototype.getItem = function (key) {
    if (this === window.localStorage && AUTH_STORAGE_KEYS.has(String(key))) return migrateAuthValue(String(key));
    return nativeStorage.getItem.call(this, key);
  };

  Storage.prototype.setItem = function (key, value) {
    if (this === window.localStorage && AUTH_STORAGE_KEYS.has(String(key))) {
      storageSet(window.sessionStorage, String(key), String(value));
      storageRemove(window.localStorage, String(key));
      return;
    }
    return nativeStorage.setItem.call(this, key, value);
  };

  Storage.prototype.removeItem = function (key) {
    if (this === window.localStorage && AUTH_STORAGE_KEYS.has(String(key))) {
      storageRemove(window.sessionStorage, String(key));
      storageRemove(window.localStorage, String(key));
      return;
    }
    return nativeStorage.removeItem.call(this, key);
  };

  AUTH_STORAGE_KEYS.forEach(migrateAuthValue);

  function isSafeUrl(raw) {
    const value = String(raw || "").trim();
    if (!value) return true;
    if (value.startsWith("#") || value.startsWith("/") || value.startsWith("?")) return true;
    if (/^[./][^:]*$/u.test(value)) return true;
    try {
      return SAFE_URL_PROTOCOLS.has(new URL(value, window.location.origin).protocol);
    } catch {
      return false;
    }
  }

  function isSafeInlineHandler(value) {
    const source = String(value || "").trim();
    const match = source.match(/^([A-Za-z_$][\w$]*)\((.*)\)$/s);
    if (!match || !SAFE_INLINE_HANDLERS.has(match[1])) return false;
    return !/[`;{}<>]|javascript\s*:|data\s*:/i.test(match[2]);
  }

  function sanitizeAttributes(el) {
    for (const attr of Array.from(el.attributes || [])) {
      const name = attr.name.toLowerCase();
      const value = attr.value || "";
      if (name.startsWith("on")) {
        if (name !== "onclick" || !isSafeInlineHandler(value)) el.removeAttribute(attr.name);
        continue;
      }
      if (["href", "src", "xlink:href", "action", "formaction"].includes(name) && !isSafeUrl(value)) {
        el.removeAttribute(attr.name);
        el.setAttribute("data-blocked-url", "unsafe");
        continue;
      }
      if (name === "style" && /expression\s*\(|javascript\s*:|data\s*:/i.test(value)) {
        el.removeAttribute(attr.name);
      }
    }
    if (el.tagName === "A" && el.getAttribute("target") === "_blank" && !el.getAttribute("rel")) {
      el.setAttribute("rel", "noopener noreferrer");
    }
  }

  function sanitizeHTML(html) {
    const template = document.createElement("template");
    nativeInnerHTML.set.call(template, String(html ?? ""));
    const blocked = template.content.querySelectorAll("script,iframe,object,embed,base,meta,link");
    blocked.forEach((node) => node.remove());
    template.content.querySelectorAll("*").forEach(sanitizeAttributes);
    return nativeInnerHTML.get.call(template);
  }

  Object.defineProperty(Element.prototype, "innerHTML", {
    configurable: true,
    enumerable: nativeInnerHTML.enumerable,
    get: nativeInnerHTML.get,
    set(value) {
      nativeInnerHTML.set.call(this, sanitizeHTML(value));
    },
  });

  Element.prototype.insertAdjacentHTML = function (position, text) {
    return nativeInsertAdjacentHTML.call(this, position, sanitizeHTML(text));
  };

  Element.prototype.setAttribute = function (name, value) {
    const attr = String(name || "").toLowerCase();
    if (attr.startsWith("on") && (attr !== "onclick" || !isSafeInlineHandler(value))) return;
    if (["href", "src", "xlink:href", "action", "formaction"].includes(attr) && !isSafeUrl(value)) {
      try { this.removeAttribute(name); } catch { /* 忽略不可移除属性 */ }
      return nativeSetAttribute.call(this, "data-blocked-url", "unsafe");
    }
    return nativeSetAttribute.call(this, name, value);
  };

  window.frontendSecurity = Object.freeze({ isSafeUrl, isSafeInlineHandler, sanitizeHTML });
})();
