(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const $ = (id) => document.getElementById(id);

  const elements = {
    userName: $("user-name"),
    roundCount: $("round-count"),
    progressFill: $("progress-fill"),
    target: $("target-number"),
    options: $("options"),
    questionArea: $("question-area"),
    completeArea: $("complete-area"),
    adStatus: $("ad-status"),
    adStatusTitle: $("ad-status-title"),
    adStatusCopy: $("ad-status-copy"),
    feedback: $("feedback"),
    notice: $("notice"),
    noticeTitle: $("notice-title"),
    noticeCopy: $("notice-copy"),
    retryAd: $("retry-ad"),
    nextRound: $("next-round")
  };

  const state = {
    blockId: "",
    adController: null,
    user: null,
    question: null,
    questionIndex: 0,
    challengeCompleted: false,
    pendingAnswer: null,
    adBusy: false
  };

  function showNotice(title, copy, retry = false) {
    elements.noticeTitle.textContent = title;
    elements.noticeCopy.textContent = copy;
    elements.retryAd.classList.toggle("hidden", !retry);
    elements.notice.classList.remove("hidden");
  }

  function hideNotice() {
    elements.notice.classList.add("hidden");
  }

  function randomInt(min, max) {
    const range = max - min + 1;
    const values = new Uint32Array(1);
    crypto.getRandomValues(values);
    return min + (values[0] % range);
  }

  function makeQuestion() {
    const target = randomInt(12, 36);
    const first = randomInt(Math.max(1, target - 18), Math.min(18, target - 1));
    const correct = { a: first, b: target - first, value: target, correct: true };
    const wrongPairs = [];
    const sums = new Set([target]);

    while (wrongPairs.length < 2) {
      const a = randomInt(2, 18);
      const b = randomInt(2, 18);
      const sum = a + b;
      if (sums.has(sum)) continue;
      sums.add(sum);
      wrongPairs.push({ a, b, value: sum, correct: false });
    }

    const choices = [correct, ...wrongPairs];
    for (let i = choices.length - 1; i > 0; i--) {
      const j = randomInt(0, i);
      [choices[i], choices[j]] = [choices[j], choices[i]];
    }
    return { target, choices, correctIndex: choices.findIndex(choice => choice.correct) };
  }

  function setQuestion() {
    state.question = makeQuestion();
    state.challengeCompleted = false;
    elements.questionArea.classList.remove("hidden");
    elements.completeArea.classList.add("hidden");
    elements.adStatus.classList.add("hidden");
    elements.target.innerHTML = String(state.question.target) + "<span>?</span>";
    elements.roundCount.textContent = `QUESTION ${state.questionIndex + 1} / 2`;
    elements.progressFill.style.width = state.questionIndex === 0 ? "50%" : "100%";
    elements.feedback.textContent = "Choose one answer. A full sponsored ad is required after every attempt.";
    elements.feedback.className = "feedback";
    elements.options.innerHTML = "";

    state.question.choices.forEach((choice, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "option";
      button.innerHTML =
        `<span class="option-letter">${String.fromCharCode(65 + index)}</span>` +
        `<span class="option-value">${choice.a} + ${choice.b}</span>` +
        '<span class="option-arrow">↗</span>';
      button.addEventListener("click", () => handleAnswer(index));
      elements.options.appendChild(button);
    });
  }

  function showAdScreen() {
    elements.questionArea.classList.add("hidden");
    elements.completeArea.classList.add("hidden");
    elements.adStatus.classList.remove("hidden");
    elements.adStatusTitle.textContent = "Sponsored ad";
    elements.adStatusCopy.textContent = "Watch the full ad to unlock the result and continue. You do not need to click the ad.";
  }

  function showChallengeComplete() {
    elements.questionArea.classList.add("hidden");
    elements.adStatus.classList.add("hidden");
    elements.completeArea.classList.remove("hidden");
    elements.roundCount.textContent = "2 / 2 COMPLETED";
    elements.progressFill.style.width = "100%";
  }

  async function showRewardedAd() {
    if (!state.blockId) {
      throw new Error("AdsGram is not connected yet. The owner must add the AdsGram block ID.");
    }
    if (!window.Adsgram || typeof window.Adsgram.init !== "function") {
      throw new Error("AdsGram could not load. Check your connection and try again.");
    }
    if (!state.adController) {
      state.adController = window.Adsgram.init({ blockId: state.blockId });
    }

    let rewardEvent = false;
    try {
      state.adController.addEventListener("onReward", () => { rewardEvent = true; });
    } catch (_) {
      // The SDK promise below remains the source of completion.
    }

    const result = await state.adController.show();
    if (result && result.done === false && !rewardEvent) {
      throw new Error("The ad was not completed. Watch it to the end to continue.");
    }
    if (result && result.error) {
      throw new Error("The ad could not be completed. Please try again.");
    }
    if (!rewardEvent && !(result && result.done === true)) {
      throw new Error("The ad completion could not be confirmed. Please try again.");
    }
    return true;
  }

  function unlockAfterAd() {
    const answer = state.pendingAnswer;
    state.pendingAnswer = null;
    state.adBusy = false;
    elements.adStatus.classList.add("hidden");
    elements.questionArea.classList.remove("hidden");

    if (answer === state.question.correctIndex) {
      elements.feedback.textContent = "✓ Correct answer. Sponsored ad completed.";
      elements.feedback.className = "feedback success";
      if (state.questionIndex === 0) {
        state.questionIndex = 1;
        window.setTimeout(setQuestion, 700);
      } else {
        state.challengeCompleted = true;
        window.setTimeout(showChallengeComplete, 650);
      }
    } else {
      elements.feedback.textContent = "Not quite. Try this question again; another ad is shown after each attempt.";
      elements.feedback.className = "feedback error";
      renderEnabledOptions();
    }
  }

  function renderEnabledOptions() {
    [...elements.options.querySelectorAll("button")].forEach(button => {
      button.disabled = false;
    });
  }

  async function handleAnswer(index) {
    if (state.adBusy || state.challengeCompleted || !state.question) return;
    state.pendingAnswer = index;
    state.adBusy = true;
    [...elements.options.querySelectorAll("button")].forEach(button => { button.disabled = true; });
    hideNotice();
    showAdScreen();

    try {
      await showRewardedAd();
      unlockAfterAd();
    } catch (error) {
      state.adBusy = false;
      elements.adStatus.classList.add("hidden");
      elements.questionArea.classList.remove("hidden");
      renderEnabledOptions();
      showNotice("Ad not completed", error && error.message ? error.message : "Please try the sponsored ad again.", true);
    }
  }

  async function validateTelegramSession() {
    if (!tg || !tg.initData) {
      showNotice("Open inside Telegram", "Launch this Math CAPTCHA Mini App using the button in the USDT09 bot.");
      return false;
    }

    try {
      const response = await fetch("/api/miniapp/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ initData: tg.initData })
      });
      const data = await response.json();
      if (!response.ok || !data.ok) {
        throw new Error(data.detail || "Telegram session could not be verified.");
      }
      state.user = data.user;
      elements.userName.textContent = data.user.first_name || data.user.username || "PLAYER";
      return true;
    } catch (error) {
      showNotice("Session verification failed", error.message || "Close and reopen the Mini App from Telegram.");
      return false;
    }
  }

  async function loadConfig() {
    const response = await fetch("/api/miniapp/config", { cache: "no-store" });
    if (!response.ok) throw new Error("Mini App settings could not be loaded.");
    return response.json();
  }

  async function initialize() {
    if (tg) {
      tg.ready();
      tg.expand();
      if (tg.setHeaderColor) tg.setHeaderColor("#090d1c");
      if (tg.setBackgroundColor) tg.setBackgroundColor("#090d1c");
    }

    try {
      const [validSession, config] = await Promise.all([validateTelegramSession(), loadConfig()]);
      if (!validSession) return;
      state.blockId = String(config.adsgramBlockId || "").trim();
      setQuestion();
      if (!state.blockId) {
        showNotice("AdsGram setup required", "The Mini App is live, but real ads are not connected yet. Add the AdsGram block ID in Render as ADSGRAM_BLOCK_ID.");
      }
    } catch (error) {
      showNotice("Could not start", error.message || "Please close and reopen the Mini App.");
    }
  }

  elements.nextRound.addEventListener("click", () => {
    state.questionIndex = 0;
    state.challengeCompleted = false;
    hideNotice();
    setQuestion();
  });

  elements.retryAd.addEventListener("click", async () => {
    if (state.pendingAnswer === null || state.adBusy) return;
    hideNotice();
    state.adBusy = true;
    showAdScreen();
    try {
      await showRewardedAd();
      unlockAfterAd();
    } catch (error) {
      state.adBusy = false;
      elements.adStatus.classList.add("hidden");
      elements.questionArea.classList.remove("hidden");
      renderEnabledOptions();
      showNotice("Ad not completed", error && error.message ? error.message : "Please try again.", true);
    }
  });

  initialize();
})();