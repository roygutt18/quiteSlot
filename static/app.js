const API_BASE = window.API_BASE || "";
const apiUrl = (path) => `${API_BASE}${path}`;
let WORKING_DAYS = [];

console.log("app.js loaded");

/*************************
 * STATE + HISTORY
 *************************/
let state = {
    mode: null,
    phone: null,
    email: null,
    name: null,
    serviceId: null,
    serviceName: null,
    durationMinutes: 15,
    date: null,
    time: null,
    user: null // Global user object from session
};

let stepHistory = ["mode"];
let calendarDate = new Date();

function toLocalISODate(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
}

/*************************
 * SESSION & AUTH
 *************************/
async function checkAuth() {
    try {
        const res = await fetch(apiUrl("/api/me"));
        const data = await res.json();
        if (data.user) {
            state.user = data.user;
            state.phone = data.user.phone;
            state.name = data.user.name;
            state.email = data.user.email;
            updateHeaderUI();
        } else {
            state.user = null;
            updateHeaderUI();
        }
    } catch (e) {
        console.error("Auth check failed", e);
    }
}

function updateHeaderUI() {
    const headerName = document.getElementById("headerUserName");
    const logoutBtn = document.getElementById("logoutBtn");
    const userProfile = document.getElementById("userProfile");

    if (!headerName || !logoutBtn || !userProfile) return;

    if (state.user && state.user.name) {
        headerName.textContent = state.user.name;
        logoutBtn.classList.remove("hidden");
        userProfile.style.pointerEvents = "auto";
    } else if (state.user) {
        headerName.textContent = "פרופיל חסר";
        logoutBtn.classList.remove("hidden");
        userProfile.style.pointerEvents = "auto";
    } else {
        headerName.textContent = "התחבר";
        logoutBtn.classList.add("hidden");
        userProfile.style.pointerEvents = state.mode === 'login' ? "none" : "auto";
    }
}

async function logout() {
    return guarded("logout", async () => {
        try {
            const res = await fetch(apiUrl("/api/logout"), {
                method: "POST",
                credentials: "include"
            });
            const data = await res.json();
            if (data.success) {
                state.user = null;
                state.phone = null;
                state.name = null;

                resetWizard();
                updateHeaderUI();
            }
        } catch (e) {
            console.error("Logout failed", e);
        }
    });
}

function startLoginFlow() {
    state.mode = "login";
    goToStep("phone");
}

/*************************
 * STEP NAVIGATION
 *************************/
function goBack() {
    if (stepHistory.length <= 1) return;

    const targetIndex = stepHistory.length - 2;
    if (!state.user && stepHistory[targetIndex] === "mode") return;

    const leaving = stepHistory[stepHistory.length - 1];
    stepHistory.pop();
    const target = stepHistory[stepHistory.length - 1];

    if (leaving === "time") {
        state.time = null;
        document.querySelectorAll(".calendar-day.selected").forEach(el => el.classList.remove("selected"));
    }

    renderStep(target);

    if (target === "date") {
        renderCalendar();
    }
}

function goToStep(step) {
    const last = stepHistory[stepHistory.length - 1];
    if (last !== step) stepHistory.push(step);
    renderStep(step);
}

function resetWizard() {
    state.mode = null;
    state.date = null;
    state.time = null;
    state.serviceId = null;
    state.serviceName = null;
    stepHistory = ["mode"];
    resetUI();
    renderStep("mode");
}

async function loadServices() {
    try {
        const res = await fetch(`${API_BASE}/api/services`);
        const data = await res.json();
        return data.services || [];
    } catch (e) {
        console.error("Load services failed", e);
        return [];
    }
}

function renderServices(services) {
    const container = document.getElementById("servicesContainer");
    if (!container) return;

    container.innerHTML = "";
    services.forEach(s => {
        const card = document.createElement("div");
        card.className = "service-card";
        card.dataset.id = s.id;
        card.dataset.duration = s.duration_minutes;

        card.innerHTML = `
            <div class="service-info-main">
                <h4>${s.name}</h4>
            </div>
            <div class="service-meta">
                <span class="duration">${s.duration_minutes} דקות</span>
            </div>
        `;

        card.onclick = () => {
            document.querySelectorAll(".service-card").forEach(c => c.classList.remove("selected"));
            card.classList.add("selected");

            state.serviceId = s.id;
            state.serviceName = s.name;
            state.durationMinutes = s.duration_minutes;

            const nextBtn = document.getElementById("serviceNextBtn");
            if (nextBtn) nextBtn.disabled = false;
        };

        container.appendChild(card);
    });
}

async function loadBusinessMeta() {
    try {
        const res = await fetch(`${API_BASE}/api/services`);
        const data = await res.json();
        return data.working_days || [];
    } catch (e) {
        console.error("Load business meta failed", e);
        return [];
    }
}

function resetUI() {
    const phoneInput = document.getElementById("phoneInput");
    if (phoneInput) phoneInput.value = "";

    const otpInputs = document.querySelectorAll(".otp-inputs input");
    otpInputs.forEach(i => i.value = "");

    const nameInput = document.getElementById("nameInput");
    if (nameInput) nameInput.value = "";

    const slotsDiv = document.getElementById("slots");
    if (slotsDiv) slotsDiv.innerHTML = "";

    document.querySelectorAll(".calendar-day.selected").forEach(el => el.classList.remove("selected"));

    const cancelBox = document.getElementById("cancelList");
    if (cancelBox) cancelBox.innerHTML = "";

    hideModal();
}

function renderStep(step) {
    document.querySelectorAll(".wizard-step").forEach(s => s.classList.remove("active"));
    const stepEl = document.querySelector(`[data-step="${step}"]`);
    if (stepEl) stepEl.classList.add("active");

    updateProgress(step);
    const backBtn = document.getElementById("backBtn");
    if (backBtn) backBtn.classList.toggle("hidden", step === "mode");
}

const STEP_MAPS = {
    login: ["phone", "otp"],
    book: ["phone", "otp", "details", "service", "date", "time"],
    cancel: ["phone", "otp", "cancel-list"]
};

function updateProgress(currentStep) {
    const mode = state.mode || "login";
    let steps = STEP_MAPS[mode] || [];
    const progressEl = document.querySelector(".progress-section");
    if (!progressEl) return;

    // Filter steps that are already completed to keep the counter relevant
    if (state.user) {
        steps = steps.filter(s => s !== "phone" && s !== "otp");
        if (state.user.name) {
            steps = steps.filter(s => s !== "details");
        }
    }

    // Hide progress bar in cancel flow or mode selection
    if (currentStep === "mode" || state.mode === "cancel") {
        progressEl.style.visibility = "hidden";
        progressEl.style.display = "none";
        return;
    }
    progressEl.style.visibility = "visible";
    progressEl.style.display = "block";

    let idx = steps.indexOf(currentStep);
    if (currentStep === "mode") idx = -1;
    if (currentStep === "confirm") idx = steps.length - 1;

    const total = steps.length;
    const current = idx + 1;

    const bar = document.getElementById("progressBar");
    const text = document.getElementById("stepCounterText");

    if (bar) {
        const pct = total > 0 ? (Math.max(0, current) / total) * 100 : 0;
        bar.style.width = `${pct}%`;
    }
    if (text) {
        text.textContent = current <= 0 ? "מתחילים..." : `שלב ${current} מתוך ${total}`;
    }
}

function setButtonLoading(btn, text = "טוען…") {
    if (!btn) return;
    btn.dataset.originalText = btn.textContent;
    btn.textContent = text;
    btn.disabled = true;
    btn.classList.add("loading");
}

function clearButtonLoading(btn) {
    if (!btn) return;
    btn.textContent = btn.dataset.originalText || btn.textContent;
    btn.disabled = false;
    btn.classList.remove("loading");
}

const inFlight = new Set();
async function guarded(taskKey, fn) {
    if (inFlight.has(taskKey)) return null;
    inFlight.add(taskKey);
    try {
        return await fn();
    } finally {
        inFlight.delete(taskKey);
    }
}

function setBoxLoading(el) { if (el) el.innerHTML = "<div class='spinner'></div>"; }
function setBoxEmpty(el, html) { if (el) el.innerHTML = html; }
function clearBox(el) { if (el) el.innerHTML = ""; }

/*************************
 * MODAL (robust + confirm/cancel)
 *************************/
let _modalEscHandler = null;

function _q(id) { return document.getElementById(id); }

function showModal({
    title,
    text,
    onConfirm = null,
    onCancel = null,
    type = "info",
    hideClose = false,
    confirmText = "אישור",
    closeText = "ביטול"
}) {
    const modal = _q("modal");
    const modalTitle = _q("modalTitle");
    const modalText = _q("modalText");
    const modalConfirm = _q("modalConfirm");
    const modalClose = _q("modalClose");

    if (!modal || !modalTitle || !modalText || !modalConfirm || !modalClose) {
        console.error("Modal DOM is missing. Check index.html ids.");
        return;
    }

    modalTitle.textContent = title ?? "";
    modalText.textContent = text ?? "";

    // If modal-icon exists, color it; if not, skip safely
    const iconDiv = modal.querySelector(".modal-icon");
    if (iconDiv) {
        if (type === "success") iconDiv.style.color = "var(--success)";
        else if (type === "error") iconDiv.style.color = "var(--danger)";
        else iconDiv.style.color = "var(--primary)";
    }

    // Prevent clicks inside box from closing anything accidentally
    const modalBox = modal.querySelector(".modal-box");
    if (modalBox) modalBox.onclick = (e) => e.stopPropagation();

    // Confirm button
    if (onConfirm) {
        modalConfirm.classList.remove("hidden");
        modalConfirm.textContent = confirmText;
        modalConfirm.onclick = () => {
            hideModal();
            try { onConfirm(); } catch (e) { console.error(e); }
        };
    } else {
        modalConfirm.classList.add("hidden");
        modalConfirm.onclick = null;
    }

    // Close/Cancel button
    if (hideClose) {
        modalClose.classList.add("hidden");
        modalClose.onclick = null;
    } else {
        modalClose.classList.remove("hidden");
        modalClose.textContent = closeText;
        modalClose.onclick = () => {
            hideModal();
            if (onCancel) {
                try { onCancel(); } catch (e) { console.error(e); }
            }
        };
    }

    // Disable backdrop click (only buttons close)
    const backdrop = modal.querySelector(".modal-backdrop");
    if (backdrop) backdrop.onclick = null;

    modal.classList.remove("hidden");

    // ESC closes only if close is allowed
    if (_modalEscHandler) window.removeEventListener("keydown", _modalEscHandler);
    _modalEscHandler = (e) => {
        if (e.key === "Escape" && !hideClose) {
            hideModal();
            if (onCancel) {
                try { onCancel(); } catch (err) { console.error(err); }
            }
        }
    };
    window.addEventListener("keydown", _modalEscHandler);
}

function hideModal() {
    const modal = _q("modal");
    if (modal) modal.classList.add("hidden");
    if (_modalEscHandler) {
        window.removeEventListener("keydown", _modalEscHandler);
        _modalEscHandler = null;
    }
}


/*************************
 * TOAST
 *************************/
const toastEl = document.getElementById("toast");
let toastTimer = null;
function toast(msg, ms = 3000) {
    if (!toastEl) return;

    // Create inner content if not exists
    toastEl.innerHTML = `
        <span>${msg}</span>
        <button class="toast-close" onclick="this.parentElement.classList.add('hidden')">
            <i class="fa-solid fa-xmark"></i>
        </button>
    `;

    toastEl.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.add("hidden"), ms);
}

/*************************
 * CORE LOGIC
 *************************/
function ensureBookPrereqs() {
    if (!state.user) {
        showModal({ title: "צריך אימות", text: "יש להתחבר לפני קביעת תור", type: "error" });
        startLoginFlow();
        return false;
    }
    if (!state.user.name) {
        showModal({ title: "צריך שם", text: "יש להזין שם לפני בחירת תאריך", type: "error" });
        goToStep("details");
        return false;
    }
    return true;
}

function setMode(mode) {
    state.mode = mode;
    if (mode === "book") {
        state.serviceId = null; state.serviceName = null; state.durationMinutes = null; state.date = null; state.time = null;
        document.querySelectorAll(".service-card").forEach(c => c.classList.remove("selected"));
        const nextBtn = document.getElementById("serviceNextBtn");
        if (nextBtn) nextBtn.disabled = true;
    }
    if (!state.user) {
        startLoginFlow();
        return;
    }
    if (mode === "book") {
        if (!state.user.name) goToStep("details");
        else goToStep("service");
    } else if (mode === "cancel") {
        loadCancelAppointments();
    }
}

async function loadSlots() {
    if (!ensureBookPrereqs()) return;
    return guarded(`day-slots:${state.date}`, async () => {
        goToStep("time");
        clearSlots(true);
        try {
            const res = await fetch(apiUrl(`/api/day-slots?date=${state.date}&duration=${state.durationMinutes}`));
            const data = await res.json();
            const slotsDiv = document.getElementById("slots");
            if (!slotsDiv) return;
            slotsDiv.innerHTML = "";
            if (!data.slots?.length) {
                showModal({ title: "אין שעות פנויות", text: "נסה יום אחר", onConfirm: goBack });
                return;
            }
            data.slots.forEach(t => {
                const b = document.createElement("div");
                b.className = "slot"; b.textContent = t;
                b.onclick = () => {
                    state.time = t;
                    showModal({ title: "אישור תור", text: `לקבוע ל-${state.date} ב-${t}?`, onConfirm: submitBooking });
                };
                slotsDiv.appendChild(b);
            });
        } catch (e) {
            clearSlots(false);
            showModal({ title: "שגיאה", text: "בעיה בטעינת שעות", type: "error" });
        }
    });
}

function clearSlots(showLoading = false) {
    const slotsDiv = document.getElementById("slots");
    if (slotsDiv) slotsDiv.innerHTML = showLoading ? "<div class='spinner'></div>" : "";
}

async function submitBooking() {
    if (!ensureBookPrereqs()) return;
    return guarded(`book:${state.date}:${state.time}`, async () => {
        setButtonLoading(modalConfirm, "קובע…");
        try {
            const res = await fetch(apiUrl("/api/book"), {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ date: state.date, time: state.time, duration_minutes: state.durationMinutes, service_name: state.serviceName })
            });
            const data = await res.json();
            showModal({ title: data.ok ? "הצלחה" : "שגיאה", text: data.ok ? "התור נקבע" : data.message, onConfirm: data.ok ? resetWizard : null, type: data.ok ? "success" : "error" });
        } catch (e) {
            showModal({ title: "שגיאה", text: "שגיאה בתקשורת", type: "error" });
        } finally { clearButtonLoading(modalConfirm); }
    });
}

async function loadCancelAppointments() {
    if (!state.user) { startLoginFlow(); return; }
    return guarded("cancel-list", async () => {
        goToStep("cancel-list");
        const box = document.getElementById("cancelList");
        if (!box) return;
        box.classList.remove("slots-grid");
        box.classList.add("cancel-list");
        setBoxLoading(box);
        try {
            const res = await fetch(apiUrl("/api/cancel/list"));
            const data = await res.json();
            clearBox(box);
            if (!data.appointments?.length) {
                setBoxEmpty(box, "<div class='empty-msg' style='text-align:center; padding:2rem; color:var(--text-muted); font-weight:600;'>אין תורים לביטול</div>");
                return;
            }
            data.appointments.forEach(a => {
                const item = document.createElement("div");
                item.className = "cancel-item";
                const dateObj = new Date(a.start);
                const dateStr = dateObj.toLocaleDateString("he-IL", { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
                const timeStr = dateObj.toLocaleTimeString("he-IL", { hour: '2-digit', minute: '2-digit' });

                item.innerHTML = `
                    <div class="cancel-info">
                        <span class="cancel-date">${dateStr || 'תאריך לא ידוע'}</span>
                        <span class="cancel-time">${timeStr || '--:--'} - ${a.service_name || 'שירות'}</span>
                    </div>
                    <button class="cancel-btn">ביטול תור</button>
                `;
                const btn = item.querySelector(".cancel-btn");
                btn.onclick = () => {
                    showModal({
                        title: "ביטול תור",
                        text: `בטוח שברצונך לבטל את התור ב-${dateStr || 'לא ידוע'} בשעה ${timeStr || '---'}?`,
                        onConfirm: async () => {
                            return guarded(`cancel:${a.id}`, async () => {
                                setButtonLoading(modalConfirm, "מבטל…");
                                try {
                                    const r = await fetch(apiUrl("/api/cancel"), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: a.id }) });
                                    const out = await r.json();
                                    showModal({ title: out.ok ? "בוטל בהצלחה" : "שגיאה", text: out.ok ? "התור הוסר מהמערכת" : out.message, onConfirm: out.ok ? resetWizard : null, type: out.ok ? "success" : "error" });
                                } finally { clearButtonLoading(modalConfirm); }
                            });
                        }
                    });
                };
                box.appendChild(item);
            });
        } catch (e) { setBoxEmpty(box, "שגיאה בטעינת תורים"); }
    });
}

/*************************
 * CALENDAR
 *************************/
function renderCalendar() {
    const grid = document.getElementById("calendar");
    const title = document.getElementById("calendarTitle");
    if (!grid || !title) return;
    grid.innerHTML = "";
    const year = calendarDate.getFullYear();
    const month = calendarDate.getMonth();
    title.textContent = calendarDate.toLocaleDateString("he-IL", { month: "long", year: "numeric" });
    const firstDay = new Date(year, month, 1);
    const startOffset = firstDay.getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const totalCells = Math.ceil((startOffset + daysInMonth) / 7) * 7;
    for (let i = 0; i < totalCells; i++) {
        const d = new Date(year, month, i - startOffset + 1); d.setHours(0, 0, 0, 0);
        const el = document.createElement("div"); el.className = "calendar-day"; el.textContent = d.getDate();
        const iso = toLocalISODate(d);
        const isPast = d < today; const isWorking = WORKING_DAYS.includes(d.getDay());
        if (d.getMonth() !== month) el.classList.add("other-month");
        if (isPast || !isWorking || d.getMonth() !== month) el.classList.add("disabled");
        else {
            el.onclick = () => {
                document.querySelectorAll(".calendar-day").forEach(x => x.classList.remove("selected"));
                el.classList.add("selected"); state.date = iso; loadSlots();
            };
        }
        if (state.date === iso) el.classList.add("selected");
        grid.appendChild(el);
    }
}

/*************************
 * DOM READY
 *************************/
document.addEventListener("DOMContentLoaded", () => {
    console.log("DOM fully loaded");

    // Bind Mode Cards
    document.querySelectorAll(".mode-card").forEach(card => {
        card.onclick = () => setMode(card.dataset.mode);
    });

    // Back Button
    const backBtn = document.getElementById("backBtn");
    if (backBtn) backBtn.onclick = goBack;

    // Profile & Logout
    const profile = document.getElementById("userProfile");
    if (profile) profile.onclick = () => { if (!state.user) startLoginFlow(); };
    const logoutBtn = document.getElementById("logoutBtn");
    if (logoutBtn) {
        logoutBtn.onclick = (e) => {
            e.stopPropagation();
            showModal({
                title: "התנתקות",
                text: "בטוח שברצונך להתנתק?",
                type: "info",
                confirmText: "כן, להתנתק",
                closeText: "ביטול",
                onConfirm: () => logout(),
                onCancel: () => { }
            });
        };
    }



    // OTP Box Logic
    const otpInputs = document.querySelectorAll(".otp-inputs input");
    otpInputs.forEach((input, index) => {
        input.oninput = (e) => {
            const val = input.value;
            if (val && val.length > 1) {
                input.value = val[0];
            }
            if (input.value && index < otpInputs.length - 1) {
                otpInputs[index + 1].focus();
            }
        };
        input.onkeydown = (e) => {
            if (e.key === "Backspace" && !input.value && index > 0) {
                otpInputs[index - 1].focus();
            }
        };
        input.onpaste = (e) => {
            e.preventDefault();
            const data = (e.clipboardData || window.clipboardData).getData("text").trim().replace(/\D/g, "");
            if (data) {
                data.split("").forEach((char, i) => {
                    if (otpInputs[index + i]) {
                        otpInputs[index + i].value = char;
                    }
                });
                const nextIdx = Math.min(index + data.length, otpInputs.length - 1);
                otpInputs[nextIdx].focus();
            }
        };
    });

    // OTP: Send Code
    const sendCodeBtn = document.getElementById("sendCodeBtn");
    const phoneInput = document.getElementById("phoneInput");
    if (sendCodeBtn && phoneInput) {
        sendCodeBtn.onclick = async () => {
            const phone = phoneInput.value.trim();
            if (!phone) { showModal({ title: "שגיאה", text: "יש להזין מספר טלפון", type: "error" }); return; }
            setButtonLoading(sendCodeBtn, "שולח...");
            try {
                const res = await fetch(apiUrl("/api/auth/start"), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ phone })
                });
                const out = await res.json();
                if (out.ok) {
                    state.phone = phone;
                    goToStep("otp");
                    startOtpCooldown();
                    setTimeout(() => otpInputs[0]?.focus(), 100);
                } else {
                    showModal({ title: "שגיאה", text: out.message, type: "error" });
                }
            } catch (e) {
                showModal({ title: "שגיאה", text: "שגיאה בתקשורת. נסה שוב.", type: "error" });
            } finally {
                clearButtonLoading(sendCodeBtn);
            }
        };
    }

    // OTP: Verify Code
    const verifyCodeBtn = document.getElementById("verifyCodeBtn");
    if (verifyCodeBtn) {
        verifyCodeBtn.onclick = async () => {
            const code = Array.from(otpInputs).map(i => i.value).join("");
            const name = document.getElementById("otpNameInput")?.value.trim();

            if (code.length < 6) { showModal({ title: "שגיאה", text: "יש להזין קוד מלא (6 ספרות)", type: "error" }); return; }

            setButtonLoading(verifyCodeBtn, "מאמת...");
            try {
                const res = await fetch(apiUrl("/api/auth/verify"), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ phone: state.phone, code, name })
                });
                const data = await res.json();
                if (data.ok) {
                    state.user = data.user;
                    state.name = data.user.name;
                    updateHeaderUI();
                    if (!state.name) {
                        toast("כמעט שם! המשך קצר לפרטים אישיים");
                        goToStep("details");
                        return;
                    }
                    toast("התחברת בהצלחה");

                    if (state.mode === "login" || (state.mode === "book" && state.name)) {
                        resetWizard();
                    } else if (state.mode === "book") {
                        goToStep("service");
                    } else if (state.mode === "cancel") {
                        loadCancelAppointments();
                    }
                } else {
                    showModal({ title: "שגיאה", text: data.message, type: "error" });
                }
            } catch (e) {
                showModal({ title: "שגיאה", text: "שגיאה בתקשורת. נסה שוב.", type: "error" });
            } finally {
                clearButtonLoading(verifyCodeBtn);
            }
        };
    }

    const resendCodeBtn = document.getElementById("resendCodeBtn");
    if (resendCodeBtn) {
        resendCodeBtn.onclick = () => sendCodeBtn?.click();
    }

    // Details Path
    const detailsNextBtn = document.getElementById("detailsNextBtn");
    if (detailsNextBtn) {
        detailsNextBtn.onclick = async () => {
            const nameInput = document.getElementById("nameInput");
            if (!nameInput) return;
            const name = nameInput.value.trim();
            if (!name) { showModal({ title: "שגיאה", text: "יש להזין שם", type: "error" }); return; }
            setButtonLoading(detailsNextBtn, "שומר...");
            try {
                const res = await fetch(apiUrl("/api/profile"), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
                const data = await res.json();
                if (data.ok) { state.name = name; if (state.user) state.user.name = name; updateHeaderUI(); if (state.mode === "login") resetWizard(); else goToStep("service"); }
                else showModal({ title: "שגיאה", text: data.message, type: "error" });
            } catch (e) { showModal({ title: "שגיאה", text: "שגיאה ברשת", type: "error" }); }
            finally { clearButtonLoading(detailsNextBtn); }
        };
    }

    // Service Next
    const serviceNextBtn = document.getElementById("serviceNextBtn");
    if (serviceNextBtn) serviceNextBtn.onclick = () => { if (state.serviceId) goToStep("date"); };

    // Calendar Nav
    const prevMonth = document.getElementById("prevMonth");
    const nextMonth = document.getElementById("nextMonth");
    if (prevMonth) prevMonth.onclick = () => { calendarDate = new Date(calendarDate.getFullYear(), calendarDate.getMonth() - 1, 1); renderCalendar(); };
    if (nextMonth) nextMonth.onclick = () => { calendarDate = new Date(calendarDate.getFullYear(), calendarDate.getMonth() + 1, 1); renderCalendar(); };

    // Initial Load
    checkAuth();
    loadBusinessMeta().then(days => {
        const map = { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 };
        WORKING_DAYS = days.map(d => map[d]).filter(x => x !== undefined);
        renderCalendar();
    });
    loadServices().then(renderServices);

    resetWizard();
});

let otpCooldown = 0;
let otpInterval = null;
function startOtpCooldown() {
    otpCooldown = 120;
    const btn = document.getElementById("resendCodeBtn");
    const timer = document.getElementById("otpCooldownTimer");
    if (!btn || !timer) return;
    btn.disabled = true;
    clearInterval(otpInterval);
    otpInterval = setInterval(() => {
        otpCooldown--;
        if (timer) timer.textContent = `(${otpCooldown}s)`;
        if (otpCooldown <= 0) {
            clearInterval(otpInterval);
            if (timer) timer.textContent = "";
            btn.disabled = false;
        }
    }, 1000);
}
