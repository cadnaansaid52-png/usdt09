(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const $ = (id) => document.getElementById(id);

  const el = {
    userName: $("user-name"),
    roundCount: $("round-count"),
    progressFill: $("progress-fill"),
    target: $("target-number"),
    options: $("options"),
    questionArea: $("question-area"),
    completeArea: $("complete-area"),
    bonusAmount: $("bonus-amount"),
    bonusBalance: $("bonus-balance"),
    okButton: $("ok-button"),
    adStatus: $("ad-status"),
    adStatusTitle: $("ad-status-title"),
    adStatusCopy: $("ad-status-copy"),
    feedback: $("feedback"),
    notice: $("notice"),
    noticeTitle: $("notice-title"),
    noticeCopy: $("notice-copy"),
    retryButton: $("retry-button"),
    finishButton: $("finish-button")
  };

  const state = {
    initData: "",
    challengeId: "",
    blockId: "",
    question: null,
    adOpened: false,
    verified: false
  };

  function showNotice(title, copy, retry) {
    el.noticeTitle.textContent = title;
    el.noticeCopy.textContent = copy;
    el.retryButton.classList.toggle("hidden", !retry);
    el.notice.classList.remove("hidden");
  }

  function hideNotice() {
    el.notice.classList.add("hidden");
  }

  function formatRemaining(seconds) {
    seconds = Math.max(0, Number(seconds) || 0);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    if (hours) return hours + "h " + minutes + "m";
    if (minutes) return minutes + "m " + secs + "s";
    return secs + "s";
  }

  async function postJson(path, payload) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.detail || "Something went wrong. Please try again.");
    }
    return data;
  }

  function displayQuestion(question) {
    state.question = question;
    el.questionArea.classList.remove("hidden");
    el.completeArea.classList.add("hidden");
    el.adStatus.classList.add("hidden");
    el.roundCount.textContent = "QUESTION " + question.number + " / " + question.total;
    el.progressFill.style.width = question.number === 1 ? "50%" : "100%";
    el.target.textContent = String(question.target);
    const questionMark = document.createElement("span");
    questionMark.textContent = "?";
    el.target.appendChild(questionMark);
    el.feedback.textContent = "Choose the correct expression.";
    el.feedback.className = "feedback";
    el.options.innerHTML = "";

    question.options.forEach((expression, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "option";
      const letter = document.createElement("span");
      letter.className = "option-letter";
      letter.textContent = String.fromCharCode(65 + index);
      const value = document.createElement("span");
      value.className = "option-value";
      value.textContent = expression;
      const arrow = document.createElement("span");
      arrow.className = "option-arrow";
      arrow.textContent = "↗";
      button.append(letter, value, arrow);
      button.addEventListener("click", () => submitAnswer(index, button));
      el.options.appendChild(button);
    });
  }

  function showCooldown(seconds) {
    el.questionArea.classList.add("hidden");
    el.completeArea.classList.add("hidden");
    el.adStatus.classList.add("hidden");
    showNotice(
      "Bonus already claimed",
      "Your next hourly bonus will be available in " + formatRemaining(seconds) + ".",
      false
    );
  }

  function showVerified(data) {
    state.verified = true;
    el.questionArea.classList.add("hidden");
    el.adStatus.classList.add("hidden");
    el.completeArea.classList.remove("hidden");
    el.roundCount.textContent = "VERIFIED";
    el.progressFill.style.width = "100%";
    el.bonusAmount.textContent = "+" + data.bonusAmount + " USDT";
    el.bonusBalance.textContent = "New balance: " + data.balance + " USDT";
    const optionalAdCopy = document.querySelector(".optional-ad-copy");
    if (optionalAdCopy) {
      optionalAdCopy.textContent = "Your bonus is already in your balance. Tap OK only if you want to open an optional sponsored ad. You can also close this page; your bonus will stay.";
    }
    el.okButton.classList.remove("hidden");
    el.okButton.disabled = false;
    hideNotice();
  }

  async function submitAnswer(answerIndex, clickedButton) {
    if (!state.challengeId || !state.question) return;
    const buttons = [...el.options.querySelectorAll("button")];
    buttons.forEach(button => { button.disabled = true; });
    el.feedback.textContent = "Checking your answer…";
    el.feedback.className = "feedback";

    try {
      const result = await postJson("/api/miniapp/answer", {
        initData: state.initData,
        challengeId: state.challengeId,
        questionNumber: state.question.number,
        answerIndex: answerIndex
      });

      if (result.status === "incorrect") {
        el.feedback.textContent = "Not quite. Try again.";
        el.feedback.className = "feedback error";
        buttons.forEach(button => { button.disabled = false; });
        return;
      }
      if (result.status === "next") {
        el.feedback.textContent = "✓ Correct. One more question.";
        el.feedback.className = "feedback success";
        window.setTimeout(() => displayQuestion(result.question), 450);
        return;
      }
      if (result.status === "verified") {
        showVerified(result);
        return;
      }
      if (result.status === "cooldown") {
        showCooldown(result.remainingSeconds);
        return;
      }
      throw new Error("The challenge response was not recognised.");
    } catch (error) {
      buttons.forEach(button => { button.disabled = false; });
      el.feedback.textContent = error.message || "Could not check the answer.";
      el.feedback.className = "feedback error";
    }
  }

  async function openOptionalAd() {
    if (!state.verified || state.adOpened) return;
    state.adOpened = true;
    hideNotice();
    el.completeArea.classList.add("hidden");
    el.adStatus.classList.remove("hidden");
    el.adStatusTitle.textContent = "Optional sponsored ad";
    el.adStatusCopy.textContent = "Your bonus has already been credited. You can close or skip the ad at any time.";
    el.finishButton.classList.add("hidden");

    if (!state.blockId || !window.Adsgram || typeof window.Adsgram.init !== "function") {
      el.adStatusTitle.textContent = "Your bonus is ready";
      el.adStatusCopy.textContent = "No sponsored ad is available right now. Your bonus is already in your USDT09 balance.";
      el.finishButton.classList.remove("hidden");
      return;
    }

    try {
      const controller = window.Adsgram.init({ blockId: state.blockId });
      await controller.show();
      el.adStatusTitle.textContent = "Thank you";
      el.adStatusCopy.textContent = "Your bonus was credited before the ad. You can return to USDT09 now.";
    } catch (_) {
      el.adStatusTitle.textContent = "Your bonus is safe";
      el.adStatusCopy.textContent = "The optional ad was not available or was closed. Your bonus is already in your USDT09 balance.";
    }
    el.finishButton.classList.remove("hidden");
  }

  async function initialize() {
    if (!tg || !tg.initData) {
      showNotice("Open inside Telegram", "Open USDT09 and press 🎁 Bonus, then tap the Math CAPTCHA button.", false);
      return;
    }

    tg.ready();
    tg.expand();
    if (tg.setHeaderColor) tg.setHeaderColor("#090d1c");
    if (tg.setBackgroundColor) tg.setBackgroundColor("#090d1c");
    state.initData = tg.initData;

    try {
      const configResponse = await fetch("/api/miniapp/config", { cache: "no-store" });
      const config = await configResponse.json();
      state.blockId = String(config.adsgramBlockId || "").trim();

      const session = await postJson("/api/miniapp/session", { initData: state.initData });
      const user = session.user || (tg.initDataUnsafe && tg.initDataUnsafe.user) || {};
      el.userName.textContent = user.first_name || user.username || "PLAYER";

      const urlParams = new URLSearchParams(window.location.search);
      if (urlParams.get("open_ad") === "1") {
        state.verified = true;
        await openOptionalAd();
        return;
      }

      const start = await postJson("/api/miniapp/start", { initData: state.initData });
      if (start.status === "cooldown") {
        showCooldown(start.remainingSeconds);
        return;
      }
      state.challengeId = start.challengeId;
      displayQuestion(start.question);
    } catch (error) {
      showNotice("Could not start verification", error.message || "Close and reopen the Mini App from the Bonus button.", true);
    }
  }

  el.okButton.addEventListener("click", openOptionalAd);
  el.finishButton.addEventListener("click", () => {
    if (tg && typeof tg.close === "function") tg.close();
  });
  el.retryButton.addEventListener("click", () => {
    hideNotice();
    initialize();
  });

  initialize();
})();