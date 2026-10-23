/**
 * Расписание КТМУ — Telegram Mini App (WebApp)
 * Клиентская логика, динамическое переключение недель, Live-виджет и расписание звонков.
 */

(function () {
  'use strict';

  // Защита и авто-прокси: если скрипт запущен хостингом через Node.js вместо браузера
  if (typeof window === 'undefined') {
    console.log('[Bothost Auto-launcher] webapp/app.js запущен через Node.js.');
    console.log('[Bothost Auto-launcher] Перенаправляем выполнение на основной файл бота: python main.py...');
    try {
      const { spawn } = require('child_process');
      const pyCmd = process.platform === 'win32' ? 'python' : 'python3';
      const py = spawn(pyCmd, ['main.py'], { stdio: 'inherit' });
      py.on('error', (err) => {
        console.warn(`[Bothost Auto-launcher] Не удалось запустить через ${pyCmd}, пробуем 'python':`, err.message);
        const fallback = spawn('python', ['main.py'], { stdio: 'inherit' });
        fallback.on('error', (e) => {
          console.error('[Bothost] Пожалуйста, в панели Bothost во вкладке «Запуск» смените главный файл на main.py!', e);
        });
      });
    } catch (e) {
      console.error('webapp/app.js — это клиентский скрипт Mini App. В панели Bothost во вкладке «Запуск» установите главный файл: main.py');
    }
    return;
  }

  // --- Telegram WebApp SDK ---
  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    if (tg.colorScheme === 'light') {
      document.body.classList.add('light-theme');
    }
  }

  // --- База данных расписания (загружается из schedule-db.json) ---
  let scheduleDb = null;

  // --- Состояние приложения ---
  const state = {
    userId: null,
    groupName: '1-ИС-1',
    weekOffset: 0, // Смещение в неделях относительно текущей (0 = текущая, +1 = следующая, -1 = предыдущая)
    weekNumber: 1,
    isEvenWeek: false,
    subgroup: 0, // 0 = Все, 1 = 1-я подгруппа, 2 = 2-я подгруппа
    subgroupChosen: false,
    selectedDayIndex: 0, // 0 = Пн, 1 = Вт, ... 5 = Сб
    searchQuery: '',
    weekDays: [],
    skippedPairs: new Set(), // Набор "YYYY-MM-DD:pairNum"
    monthlySkippedTotal: 0,
    selectedCourseFilter: 0, // 0 = Все, 1..4 = курсы
    groupSearchQuery: '',
    selectedCorpusTab: 'main', // 'main' (Мойка) или 'voz' (Вознесенский)
    timerInterval: null
  };

  const RU_WEEKDAYS = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота'];
  const RU_WEEKDAYS_SHORT = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'];
  const MONTHS_RU = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
  const MONTHS_GENITIVE_SHORT = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

  const DEFAULT_BELLS_MAIN = [
    { num: 1, start: '08:30', end: '10:00' },
    { num: 2, start: '10:10', end: '11:40' },
    { num: 3, start: '11:50', end: '13:20' },
    { num: 4, start: '14:00', end: '15:30' },
    { num: 5, start: '15:40', end: '17:10' },
    { num: 6, start: '17:20', end: '18:50' },
    { num: 7, start: '19:00', end: '20:30' }
  ];

  const DEFAULT_BELLS_VOZ = [
    { num: 1, start: '08:30', end: '09:55' },
    { num: 2, start: '10:05', end: '11:30' },
    { num: 3, start: '11:40', end: '13:05' },
    { num: 4, start: '13:45', end: '15:10' },
    { num: 5, start: '15:20', end: '16:45' },
    { num: 6, start: '16:55', end: '18:20' },
    { num: 7, start: '18:30', end: '20:00' }
  ];

  // --- Тактильный отклик (Haptics) ---
  function triggerHaptic(type = 'light') {
    if (tg?.HapticFeedback) {
      if (type === 'selection') {
        tg.HapticFeedback.selectionChanged();
      } else if (type === 'success') {
        tg.HapticFeedback.notificationOccurred('success');
      } else {
        tg.HapticFeedback.impactOccurred(type);
      }
    }
  }

  // --- Toast сообщения ---
  function showToast(text) {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = text;
    toast.classList.add('show');
    setTimeout(() => {
      toast.classList.remove('show');
    }, 2400);
  }

  // --- Вычисление учебной недели и четности ---
  function calculateAcademicWeekForDate(targetDate) {
    const semStartStr = scheduleDb?.config?.semesterStart || '2026-09-01';
    let semStart;
    try {
      const parts = semStartStr.split('-');
      semStart = new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10));
    } catch (e) {
      semStart = new Date(targetDate.getFullYear(), 8, 1);
    }

    const startMonday = new Date(semStart);
    const startDay = (startMonday.getDay() + 6) % 7;
    startMonday.setDate(startMonday.getDate() - startDay);
    startMonday.setHours(0, 0, 0, 0);

    const targetMonday = new Date(targetDate);
    const targetDay = (targetMonday.getDay() + 6) % 7;
    targetMonday.setDate(targetMonday.getDate() - targetDay);
    targetMonday.setHours(0, 0, 0, 0);

    const diffMs = targetMonday - startMonday;
    const diffDays = Math.round(diffMs / (1000 * 60 * 60 * 24));
    let weekNum = Math.floor(diffDays / 7) + 1;
    if (weekNum < 1) weekNum = 1;

    // Четность недели (локальная дата без смещения часового пояса)
    const year = targetDate.getFullYear();
    const month = String(targetDate.getMonth() + 1).padStart(2, '0');
    const day = String(targetDate.getDate()).padStart(2, '0');
    const isoDate = `${year}-${month}-${day}`;
    const overrides = scheduleDb?.config?.weekParityOverrides || {};
    let isEven;
    if (overrides[isoDate]) {
      isEven = overrides[isoDate] === 'even';
    } else if (overrides[String(weekNum)]) {
      isEven = overrides[String(weekNum)] === 'even';
    } else {
      isEven = (weekNum % 2 === 0);
    }

    return { weekNumber: weekNum, isEven: isEven };
  }

  function getMondayForOffset(offsetWeeks) {
    const now = new Date();
    const currentDay = (now.getDay() + 6) % 7;
    const monday = new Date(now);
    monday.setDate(now.getDate() - currentDay + (offsetWeeks * 7));
    monday.setHours(0, 0, 0, 0);
    return monday;
  }

  // --- Обновление адресной строки ---
  function updateAddressBar() {
    try {
      const group = state.groupName || '1-ИС-1';
      let search = `?group=${encodeURIComponent(group)}`;
      if (state.subgroup && state.subgroup > 0) {
        search += `&subgroup=${state.subgroup}`;
      }
      if (state.weekOffset !== 0) {
        search += `&week_offset=${state.weekOffset}`;
      }
      if (window.history && window.history.replaceState) {
        window.history.replaceState(null, '', window.location.pathname + search);
      }
    } catch (e) {}
  }

  function getCleanShareUrl() {
    const origin = window.location.origin || 'https://ktmu-schedule-bot-plqd-one.vercel.app';
    const group = state.groupName || '1-ИС-1';
    let url = `${origin}/?group=${encodeURIComponent(group)}`;
    if (state.subgroup && state.subgroup > 0) {
      url += `&subgroup=${state.subgroup}`;
    }
    return url;
  }

  // --- Загрузка schedule-db.json ---
  async function loadScheduleDb() {
    try {
      const resp = await fetch('schedule-db.json', { cache: 'no-cache' });
      if (resp.ok) {
        scheduleDb = await resp.json();
        console.log('[Mini App] schedule-db.json успешно загружен, групп:', scheduleDb.groups?.length);
      }
    } catch (e) {
      console.warn('[Mini App] Не удалось загрузить schedule-db.json:', e);
    }
  }

  // --- Генерация расписания недели из базы данных scheduleDb ---
  function buildWeekScheduleFromDb(groupName, weekOffset) {
    const monday = getMondayForOffset(weekOffset);
    const midWeekDate = new Date(monday);
    midWeekDate.setDate(monday.getDate() + 2); // Среда
    const weekInfo = calculateAcademicWeekForDate(midWeekDate);
    const academicWeek = weekInfo.weekNumber;
    const isEven = weekInfo.isEven;

    state.weekNumber = academicWeek;
    state.isEvenWeek = isEven;

    if (!scheduleDb || !scheduleDb.schedules) {
      return buildFallbackWeek(groupName, monday, academicWeek, isEven);
    }

    const groupSchedule = scheduleDb.schedules[groupName];
    if (!groupSchedule) {
      return buildFallbackWeek(groupName, monday, academicWeek, isEven);
    }

    const periodTimes = scheduleDb.config?.periodTimes || DEFAULT_BELLS_MAIN.map(b => `${b.start}-${b.end}`);
    const vozPeriodTimes = scheduleDb.config?.voznesenskyPeriodTimes || DEFAULT_BELLS_VOZ.map(b => `${b.start}-${b.end}`);

    const days = [];
    for (let dayIdx = 0; dayIdx < 6; dayIdx++) {
      const d = new Date(monday);
      d.setDate(monday.getDate() + dayIdx);
      const isoDate = d.toISOString().split('T')[0];

      const dayLessons = [];
      groupSchedule.forEach(item => {
        // [day, period_idx, subj_idx, teacher_idx, room_idx, type_idx, is_remote, subgroup, week_pattern, custom_weeks]
        const [dIdx, pIdx, sIdx, tIdx, rIdx, tpIdx, isRemote, subg, wp, cweeks] = item;
        if (dIdx !== dayIdx) return;

        // Проверка применимости к текущей неделе
        if (cweeks && cweeks.length > 0) {
          if (!cweeks.includes(academicWeek)) return;
        } else if (wp === 'odd' && isEven) {
          return;
        } else if (wp === 'even' && !isEven) {
          return;
        }

        const subjName = scheduleDb.subjects[sIdx] || 'Учебное занятие';
        const teacherName = scheduleDb.teachers[tIdx] || '';
        const roomData = scheduleDb.rooms[rIdx] || ['', 0];
        const roomName = roomData[0] || (isRemote ? 'Дистант' : '');
        const isExt = Boolean(roomData[1]);
        const typeName = scheduleDb.types[tpIdx] || 'Занятие';

        let rawTime = '';
        if (isExt && pIdx < vozPeriodTimes.length) {
          rawTime = vozPeriodTimes[pIdx];
        } else if (pIdx < periodTimes.length) {
          rawTime = periodTimes[pIdx];
        } else {
          rawTime = '08:30-10:00';
        }

        const timeParts = rawTime.split('-');
        dayLessons.push({
          pair_number: pIdx + 1,
          time: rawTime,
          start_time: timeParts[0] || '',
          end_time: timeParts[1] || '',
          subject: subjName,
          teacher: teacherName,
          room: roomName,
          format: isRemote ? 'Дистанционно' : 'Очно',
          subgroup: subg || 0,
          lesson_type: typeName,
          is_external: isExt,
          location: isExt ? 'Вознесенский пр., 44' : (isRemote ? 'Дистант' : 'Основной корпус')
        });
      });

      dayLessons.sort((a, b) => a.pair_number - b.pair_number || a.subgroup - b.subgroup);

      days.push({
        date: isoDate,
        day_name: RU_WEEKDAYS[dayIdx],
        week_number: academicWeek,
        is_even_week: isEven,
        is_even: isEven,
        lessons: dayLessons
      });
    }

    return days;
  }

  function buildFallbackWeek(groupName, monday, academicWeek, isEven) {
    const days = [];
    for (let dayIdx = 0; dayIdx < 6; dayIdx++) {
      const d = new Date(monday);
      d.setDate(monday.getDate() + dayIdx);
      const isoDate = d.toISOString().split('T')[0];
      days.push({
        date: isoDate,
        day_name: RU_WEEKDAYS[dayIdx],
        week_number: academicWeek,
        is_even_week: isEven,
        is_even: isEven,
        lessons: []
      });
    }
    return days;
  }

  // --- Загрузка и декодирование расписания ---
  function loadScheduleData() {
    const hash = window.location.hash;
    const urlParams = new URLSearchParams(window.location.search);
    const queryGroup = urlParams.get('group');
    const querySubgroup = urlParams.get('subgroup');
    const queryOffset = urlParams.get('week_offset');

    if (queryOffset !== null) {
      const parsedOffset = parseInt(queryOffset, 10);
      if (!isNaN(parsedOffset)) state.weekOffset = parsedOffset;
    }

    // 1. Попытка чтения из URL hash (#data=...)
    if (hash.includes('data=')) {
      try {
        const rawData = hash.split('data=')[1].split('&')[0];
        const jsonStr = decodeURIComponent(escape(atob(rawData.replace(/-/g, '+').replace(/_/g, '/'))));
        const parsed = JSON.parse(jsonStr);
        applyParsedData(parsed);
        updateAddressBar();
        return;
      } catch (e) {
        console.warn('Не удалось распарсить hash данные:', e);
      }
    }

    // 2. Чтение из URL параметров (?group=...)
    if (queryGroup) {
      state.groupName = queryGroup;
      try { localStorage.setItem('ktmu_selected_group', queryGroup); } catch (e) {}
      if (querySubgroup !== null) {
        const subNum = parseInt(querySubgroup, 10);
        if (!isNaN(subNum) && subNum >= 0) {
          state.subgroup = subNum;
          state.subgroupChosen = true;
          try {
            localStorage.setItem('ktmu_user_subgroup', String(subNum));
            localStorage.setItem('ktmu_subgroup_chosen', '1');
          } catch (e) {}
        }
      }
      refreshWeekSchedule();
      return;
    }

    // 3. Чтение из localStorage
    const savedGroup = localStorage.getItem('ktmu_selected_group');
    if (savedGroup) {
      state.groupName = savedGroup;
    }
    refreshWeekSchedule();
  }

  function applyParsedData(data) {
    state.groupName = data.group_name || data.group || localStorage.getItem('ktmu_selected_group') || '1-ИС-1';
    state.weekNumber = data.week_number || 1;
    state.isEvenWeek = data.is_even !== undefined ? Boolean(data.is_even) : (state.weekNumber % 2 === 0);
    state.weekDays = data.days || [];

    if (data.subgroup !== undefined && data.subgroup !== null) {
      state.subgroup = parseInt(data.subgroup, 10) || 0;
      try { localStorage.setItem('ktmu_user_subgroup', String(state.subgroup)); } catch (e) {}
    }
    if (data.subgroup_chosen) {
      state.subgroupChosen = true;
      try { localStorage.setItem('ktmu_subgroup_chosen', '1'); } catch (e) {}
    }
    if (data.user_id) {
      state.userId = parseInt(data.user_id, 10);
      try { localStorage.setItem('ktmu_user_id', state.userId); } catch (e) {}
    }
    if (Array.isArray(data.skipped_pairs)) {
      data.skipped_pairs.forEach(k => state.skippedPairs.add(k));
      saveLocalSkips();
    }
    try {
      localStorage.setItem('ktmu_selected_group', state.groupName);
    } catch (e) {}
  }

  function refreshWeekSchedule() {
    state.weekDays = buildWeekScheduleFromDb(state.groupName, state.weekOffset);
  }

  // --- Переключение учебной недели ---
  function changeWeek(delta) {
    triggerHaptic('light');
    state.weekOffset += delta;
    updateAddressBar();
    refreshWeekSchedule();
    renderHeader();
    renderDaysNav();
    renderSchedule();
    updateLiveWidget();
    updateTodayJumpButton();
  }

  function jumpToCurrentWeek() {
    triggerHaptic('success');
    state.weekOffset = 0;
    state.selectedDayIndex = determineDefaultDayIndex();
    updateAddressBar();
    refreshWeekSchedule();
    renderHeader();
    renderDaysNav();
    renderSchedule();
    updateLiveWidget();
    updateTodayJumpButton();
    showToast('Переключено на сегодня 📅');
  }

  function jumpToSpecificWeek(targetWeekNumber) {
    triggerHaptic('selection');
    const currentWeekInfo = calculateAcademicWeekForDate(new Date());
    state.weekOffset = targetWeekNumber - currentWeekInfo.weekNumber;
    updateAddressBar();
    refreshWeekSchedule();
    renderHeader();
    renderDaysNav();
    renderSchedule();
    updateLiveWidget();
    updateTodayJumpButton();
    closeWeekModal();
  }

  // --- Переключение группы ---
  function switchGroup(groupName) {
    triggerHaptic('selection');
    state.groupName = groupName;
    try {
      localStorage.setItem('ktmu_selected_group', groupName);
    } catch (e) {}
    updateAddressBar();
    refreshWeekSchedule();
    renderHeader();
    renderDaysNav();
    renderSchedule();
    updateLiveWidget();
    renderGroupModalList();
    showToast(`Выбрана группа ${groupName} ✅`);
  }

  // --- Синхронизация сессии пользователя и пропусков ---
  function initUserSession() {
    if (tg?.initDataUnsafe?.user?.id) {
      state.userId = tg.initDataUnsafe.user.id;
    }
    if (!state.userId) {
      const params = new URLSearchParams(window.location.search);
      const uid = params.get('user_id');
      if (uid && !isNaN(parseInt(uid, 10))) {
        state.userId = parseInt(uid, 10);
      }
    }
    if (!state.userId) {
      const saved = localStorage.getItem('ktmu_user_id');
      if (saved) state.userId = parseInt(saved, 10);
    }

    const savedSub = localStorage.getItem('ktmu_user_subgroup');
    if (savedSub !== null && !isNaN(parseInt(savedSub, 10))) {
      state.subgroup = parseInt(savedSub, 10);
    }

    if (localStorage.getItem('ktmu_subgroup_chosen') === '1') {
      state.subgroupChosen = true;
    }

    const localSkips = localStorage.getItem('ktmu_skipped_pairs');
    if (localSkips) {
      try {
        const arr = JSON.parse(localSkips);
        if (Array.isArray(arr)) {
          arr.forEach(k => state.skippedPairs.add(k));
        }
      } catch (e) {}
    }
    updateMonthlySkippedCount();
  }

  function updateMonthlySkippedCount() {
    const currentMonth = new Date().toISOString().slice(0, 7);
    let count = 0;
    state.skippedPairs.forEach(k => {
      if (k.startsWith(currentMonth)) count++;
    });
    state.monthlySkippedTotal = count;

    const badge = document.getElementById('skips-header-badge');
    if (badge) {
      if (count > 0) {
        badge.style.display = 'inline-flex';
        badge.textContent = `💤 ${count} ${count === 1 ? 'пара' : (count < 5 ? 'пары' : 'пар')}`;
      } else {
        badge.style.display = 'none';
      }
    }
  }

  function saveLocalSkips() {
    try {
      localStorage.setItem('ktmu_skipped_pairs', JSON.stringify(Array.from(state.skippedPairs)));
    } catch (e) {}
    updateMonthlySkippedCount();
  }

  function toggleSkip(dateStr, pairNum) {
    triggerHaptic('medium');
    const key = `${dateStr}:${pairNum}`;
    const willSkip = !state.skippedPairs.has(key);
    if (willSkip) {
      state.skippedPairs.add(key);
      showToast(`Пара №${pairNum} отмечена пропущенной 💤`);
    } else {
      state.skippedPairs.delete(key);
      showToast(`Пропуск пары №${pairNum} отменён ✅`);
    }
    saveLocalSkips();
    renderSchedule();

    // Фоновая синхронизация с сервером
    if (state.userId) {
      fetch('/api/skip', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: state.userId,
          date: dateStr,
          pair_number: pairNum,
          is_skipped: willSkip
        })
      }).catch(() => {});
    }
  }

  function setSubgroup(num) {
    triggerHaptic('success');
    state.subgroup = num;
    state.subgroupChosen = true;
    try {
      localStorage.setItem('ktmu_user_subgroup', String(num));
      localStorage.setItem('ktmu_subgroup_chosen', '1');
    } catch (e) {}
    updateAddressBar();
    renderSubgroupUI();
    renderDaysNav();
    renderSchedule();
    updateLiveWidget();
    const label = num === 1 ? '1-я подгруппа' : (num === 2 ? '2-я подгруппа' : 'Все пары');
    showToast(`Выбрано: ${label} ✅`);
  }

  function determineDefaultDayIndex() {
    const now = new Date();
    const dayOfWeek = (now.getDay() + 6) % 7;
    return (dayOfWeek >= 0 && dayOfWeek < 6) ? dayOfWeek : 0;
  }

  // --- Отрисовка шапки ---
  function renderHeader() {
    const titleEl = document.getElementById('group-title');
    if (titleEl) titleEl.textContent = state.groupName;

    const parityText = state.isEvenWeek ? 'чётная' : 'нечётная';
    const badgeEl = document.getElementById('week-badge');
    if (badgeEl) badgeEl.textContent = `Неделя ${state.weekNumber} • ${parityText}`;

    // Диапазон дат недели (например, 19 окт – 24 окт)
    const datesEl = document.getElementById('week-dates-range');
    if (datesEl && state.weekDays.length >= 6) {
      const dStart = state.weekDays[0].date ? state.weekDays[0].date.split('-') : null;
      const dEnd = state.weekDays[5].date ? state.weekDays[5].date.split('-') : null;
      if (dStart && dEnd && dStart.length === 3 && dEnd.length === 3) {
        const sDay = parseInt(dStart[2], 10);
        const sMonth = MONTHS_GENITIVE_SHORT[parseInt(dStart[1], 10) - 1];
        const eDay = parseInt(dEnd[2], 10);
        const eMonth = MONTHS_GENITIVE_SHORT[parseInt(dEnd[1], 10) - 1];
        datesEl.textContent = `${sDay} ${sMonth} – ${eDay} ${eMonth}`;
      }
    }
  }

  function updateTodayJumpButton() {
    const btn = document.getElementById('btn-go-today');
    if (!btn) return;
    const isTodayWeek = (state.weekOffset === 0);
    const isTodayDay = (state.selectedDayIndex === determineDefaultDayIndex());
    if (!isTodayWeek || !isTodayDay) {
      btn.style.display = 'block';
    } else {
      btn.style.display = 'none';
    }
  }

  // --- Отрисовка подгруппы ---
  function renderSubgroupUI() {
    const wrapper = document.getElementById('subgroup-notice-wrapper');
    const badge = document.getElementById('subgroup-badge');

    if (badge) {
      if (state.subgroup === 1) {
        badge.textContent = '👥 1 подгруппа ▾';
      } else if (state.subgroup === 2) {
        badge.textContent = '👥 2 подгруппа ▾';
      } else {
        badge.textContent = '👥 Все пары ▾';
      }
    }

    const hasChosen = state.subgroupChosen || localStorage.getItem('ktmu_subgroup_chosen') === '1';
    if (wrapper) {
      wrapper.style.display = hasChosen ? 'none' : 'block';
    }
  }

  // --- Отрисовка дней недели ---
  function renderDaysNav() {
    const nav = document.getElementById('days-nav');
    if (!nav) return;
    nav.innerHTML = '';

    const todayIso = new Date().toISOString().split('T')[0];

    state.weekDays.forEach((dayData, idx) => {
      const pill = document.createElement('div');
      pill.className = `day-pill ${idx === state.selectedDayIndex ? 'active' : ''}`;
      if (dayData.date === todayIso) {
        pill.classList.add('today');
      }

      let dateDisplay = '';
      if (dayData.date) {
        const parts = dayData.date.split('-');
        if (parts.length === 3) {
          dateDisplay = `${parseInt(parts[2], 10)} ${MONTHS_GENITIVE_SHORT[parseInt(parts[1], 10) - 1]}`;
        }
      }

      const count = getFilteredLessons(dayData.lessons).length;

      pill.innerHTML = `
        <span class="day-pill-name">${RU_WEEKDAYS_SHORT[idx]}</span>
        <span class="day-pill-date">${dateDisplay}</span>
        ${count > 0 ? `<span class="day-pill-badge">${count}</span>` : ''}
      `;

      pill.addEventListener('click', () => {
        triggerHaptic('selection');
        state.selectedDayIndex = idx;
        renderDaysNav();
        renderSchedule();
        updateLiveWidget();
        updateTodayJumpButton();
      });

      nav.appendChild(pill);
    });

    const activePill = nav.querySelector('.day-pill.active');
    if (activePill) {
      activePill.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
    }
  }

  // --- Фильтрация занятий ---
  function getFilteredLessons(lessons) {
    if (!lessons) return [];
    return lessons.filter(l => {
      if (state.subgroup === 1 && l.subgroup === 2) return false;
      if (state.subgroup === 2 && l.subgroup === 1) return false;

      if (state.searchQuery) {
        const q = state.searchQuery.toLowerCase();
        const s = (l.subject || '').toLowerCase();
        const t = (l.teacher || '').toLowerCase();
        const r = (l.room || '').toLowerCase();
        if (!s.includes(q) && !t.includes(q) && !r.includes(q)) {
          return false;
        }
      }
      return true;
    });
  }

  // --- Отрисовка расписания дня ---
  function renderSchedule() {
    const dayData = state.weekDays[state.selectedDayIndex];
    if (!dayData) return;

    const labelEl = document.getElementById('current-day-label');
    const dateEl = document.getElementById('current-day-date');
    if (labelEl) labelEl.textContent = dayData.day_name || RU_WEEKDAYS[state.selectedDayIndex];
    if (dateEl && dayData.date) {
      const parts = dayData.date.split('-');
      if (parts.length === 3) {
        dateEl.textContent = `${parseInt(parts[2], 10)} ${MONTHS_RU[parseInt(parts[1], 10) - 1]}`;
      }
    }

    const container = document.getElementById('lessons-timeline');
    const emptyState = document.getElementById('empty-state');
    const summaryContainer = document.getElementById('day-summary-pills');
    if (!container) return;
    container.innerHTML = '';

    const filtered = getFilteredLessons(dayData.lessons);

    // Сводка дня (количество пар, время начала и конца)
    if (summaryContainer) {
      summaryContainer.innerHTML = '';
      if (filtered.length > 0) {
        const pairWord = filtered.length === 1 ? 'пара' : (filtered.length < 5 ? 'пары' : 'пар');
        const countPill = document.createElement('span');
        countPill.className = 'summary-pill';
        countPill.textContent = `📚 ${filtered.length} ${pairWord}`;
        summaryContainer.appendChild(countPill);

        const firstStart = filtered[0].start_time || filtered[0].time?.split('-')[0] || '';
        const lastEnd = filtered[filtered.length - 1].end_time || filtered[filtered.length - 1].time?.split('-')[1] || '';
        if (firstStart && lastEnd) {
          const timePill = document.createElement('span');
          timePill.className = 'summary-pill';
          timePill.textContent = `⏰ ${firstStart} – ${lastEnd}`;
          summaryContainer.appendChild(timePill);
        }
      }
    }

    if (filtered.length === 0) {
      if (emptyState) emptyState.style.display = 'flex';
      return;
    } else {
      if (emptyState) emptyState.style.display = 'none';
    }

    const now = new Date();
    const todayIso = now.toISOString().split('T')[0];
    const isToday = (dayData.date === todayIso);
    const currentMinutes = now.getHours() * 60 + now.getMinutes();

    filtered.forEach((lesson, idx) => {
      // Добавляем разделитель перемены между парами
      if (idx > 0) {
        const prevLesson = filtered[idx - 1];
        const prevEndStr = prevLesson.end_time || prevLesson.time?.split('-')[1] || '';
        const currStartStr = lesson.start_time || lesson.time?.split('-')[0] || '';
        if (prevEndStr && currStartStr) {
          const prevEndMin = parseTimeToMinutes(prevEndStr);
          const currStartMin = parseTimeToMinutes(currStartStr);
          const breakMin = currStartMin - prevEndMin;
          if (breakMin > 0) {
            const connector = document.createElement('div');
            let breakText = `☕ Перемена ${breakMin} мин`;
            let connectorClass = 'break-connector';
            if (breakMin >= 35) {
              breakText = `🍽️ Большая перемена ${breakMin} мин (${prevEndStr}–${currStartStr})`;
              connectorClass += ' break-long';
            } else if (breakMin >= 60) {
              breakText = `⏳ Окно между парами (${Math.floor(breakMin / 60)} ч ${breakMin % 60} мин)`;
              connectorClass += ' break-window';
            }
            if (prevLesson.is_external !== lesson.is_external) {
              breakText += ' • 🏃 Смена корпуса';
            }
            connector.className = connectorClass;
            connector.textContent = breakText;
            container.appendChild(connector);
          }
        }
      }

      const card = createLessonCard(lesson, dayData.date, isToday, currentMinutes);
      container.appendChild(card);
    });
  }

  // --- Создание карточки пары ---
  function createLessonCard(lesson, dateStr, isToday, currentMinutes) {
    const pairNum = lesson.pair_number || 1;
    const skipKey = `${dateStr}:${pairNum}`;
    const isSkipped = state.skippedPairs.has(skipKey);

    let typeClass = '';
    const lType = (lesson.lesson_type || '').toLowerCase();
    if (lType.includes('лекц')) typeClass = 'type-lecture';
    else if (lType.includes('практ')) typeClass = 'type-practice';
    else if (lType.includes('лаб')) typeClass = 'type-lab';

    const room = lesson.room || (lesson.format === 'Дистанционно' ? 'Дистант' : 'Не указана');
    const roomLower = room.toLowerCase();
    const isDistant = roomLower.includes('дистант') || roomLower.includes('online') || lesson.format === 'Дистанционно';
    const isVozn = roomLower.includes('вознесен') || lesson.is_external;

    let roomBadge, roomClass;
    if (isDistant) {
      roomBadge = '🖥️ Дистант';
      roomClass = 'room-distant';
    } else if (isVozn) {
      roomBadge = `🏛️ Вознесенский: ауд. ${room}`;
      roomClass = 'room-external';
    } else {
      roomBadge = `🚪 Ауд. ${room}`;
      roomClass = '';
    }

    // Определение статуса пары на сегодня (прошла, идет сейчас, следующая)
    let liveTagHtml = '';
    let cardStatusClass = '';
    if (isToday) {
      const [startStr, endStr] = (lesson.time || '08:30-10:00').split('-');
      const startMin = parseTimeToMinutes(startStr);
      const endMin = parseTimeToMinutes(endStr);
      if (currentMinutes >= startMin && currentMinutes < endMin) {
        liveTagHtml = '<span class="lesson-live-tag tag-now">🟢 Идёт сейчас</span>';
        cardStatusClass = ' active-now';
      } else if (currentMinutes >= endMin) {
        liveTagHtml = '<span class="lesson-live-tag tag-past">✓ Прошла</span>';
        cardStatusClass = ' past-lesson';
      }
    }

    const card = document.createElement('div');
    card.className = `lesson-card${cardStatusClass}${isSkipped ? ' skipped' : ''}`;

    // Верхняя строка карточки
    const top = document.createElement('div');
    top.className = 'lesson-top';
    top.innerHTML = `
      <div class="lesson-time-badge">
        <span class="pair-number">${pairNum}</span>
        <span class="pair-time">${lesson.time || ''}</span>
      </div>
      <div class="lesson-badges-right">
        ${liveTagHtml}
        ${lesson.lesson_type ? `<span class="lesson-type-badge ${typeClass}">${lesson.lesson_type}</span>` : ''}
      </div>
    `;

    // Название предмета
    const subj = document.createElement('div');
    subj.className = 'lesson-subject';
    subj.textContent = lesson.subject || 'Учебное занятие';

    // Детали: ауд, преподаватель, подгруппа и кнопка пропуска
    const details = document.createElement('div');
    details.className = 'lesson-details';

    const metaItems = document.createElement('div');
    metaItems.className = 'lesson-meta-items';

    const roomEl = document.createElement('span');
    roomEl.className = `detail-item room${roomClass ? ' ' + roomClass : ''}`;
    roomEl.textContent = roomBadge;
    metaItems.appendChild(roomEl);

    if (lesson.teacher) {
      const teacherEl = document.createElement('span');
      teacherEl.className = 'detail-item';
      teacherEl.textContent = `👤 ${lesson.teacher}`;
      metaItems.appendChild(teacherEl);
    }

    if (lesson.subgroup) {
      const sgEl = document.createElement('span');
      sgEl.className = 'detail-item subgroup';
      sgEl.textContent = `👥 ${lesson.subgroup} подгр.`;
      metaItems.appendChild(sgEl);
    }

    const skipBtn = document.createElement('button');
    skipBtn.className = `skip-toggle-btn${isSkipped ? ' is-skipped' : ''}`;
    skipBtn.innerHTML = isSkipped ? '<span>✅</span> Пропущено' : '<span>💤</span> Пропустить';
    skipBtn.title = isSkipped ? 'Нажмите, чтобы отменить отметку пропуска' : 'Отметить пару пропущенной';
    skipBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleSkip(dateStr, pairNum);
    });

    details.appendChild(metaItems);
    details.appendChild(skipBtn);

    card.appendChild(top);
    card.appendChild(subj);
    card.appendChild(details);

    return card;
  }

  // --- Live-виджет текущей пары ---
  function updateLiveWidget() {
    const widget = document.getElementById('live-widget');
    if (!widget) return;

    const todayIso = new Date().toISOString().split('T')[0];
    const dayData = state.weekDays[state.selectedDayIndex];

    if (!dayData || dayData.date !== todayIso) {
      widget.style.display = 'none';
      return;
    }

    const lessons = getFilteredLessons(dayData.lessons);
    if (!lessons.length) {
      widget.style.display = 'none';
      return;
    }

    const now = new Date();
    const currentMinutes = now.getHours() * 60 + now.getMinutes();

    let currentLesson = null;
    let nextLesson = null;
    let isBreak = false;
    let breakEndMinutes = 0;

    for (let i = 0; i < lessons.length; i++) {
      const l = lessons[i];
      let [startStr, endStr] = (l.time || '08:30-10:00').split('-');
      if (!endStr) continue;
      const startMin = parseTimeToMinutes(startStr);
      const endMin = parseTimeToMinutes(endStr);

      if (currentMinutes >= startMin && currentMinutes < endMin) {
        currentLesson = l;
        break;
      } else if (currentMinutes < startMin) {
        nextLesson = l;
        if (i > 0) {
          const prevEnd = parseTimeToMinutes(lessons[i - 1].time?.split('-')[1] || '00:00');
          if (currentMinutes >= prevEnd && currentMinutes < startMin) {
            isBreak = true;
            breakEndMinutes = startMin;
          }
        }
        break;
      }
    }

    if (!currentLesson && !nextLesson && !isBreak) {
      widget.style.display = 'none';
      return;
    }

    widget.style.display = 'block';

    const pill = document.getElementById('live-status-pill');
    const statusText = document.getElementById('live-status-text');
    const countdown = document.getElementById('live-countdown');
    const title = document.getElementById('live-pair-title');
    const room = document.getElementById('live-room');
    const teacher = document.getElementById('live-teacher');
    const bar = document.getElementById('live-progress-bar');

    if (currentLesson) {
      const endMin = parseTimeToMinutes(currentLesson.time.split('-')[1]);
      const remain = endMin - currentMinutes;
      const startMin = parseTimeToMinutes(currentLesson.time.split('-')[0]);
      const total = endMin - startMin || 90;
      const progress = Math.min(100, Math.max(0, ((currentMinutes - startMin) / total) * 100));

      statusText.textContent = `Идет ${currentLesson.pair_number || 1} пара`;
      countdown.textContent = `Осталось ${remain} мин`;
      title.textContent = currentLesson.subject || 'Занятие';
      room.textContent = `🚪 Ауд. ${currentLesson.room || '—'}`;
      teacher.textContent = `👤 ${currentLesson.teacher || 'Преподаватель'}`;
      bar.style.width = `${progress}%`;
      pill.style.background = 'rgba(16, 185, 129, 0.15)';
      pill.style.color = 'var(--accent-emerald)';
    } else if (isBreak && nextLesson) {
      const remain = breakEndMinutes - currentMinutes;
      statusText.textContent = '☕ Перемена';
      countdown.textContent = `До пары: ${remain} мин`;
      title.textContent = `Следующая: ${nextLesson.subject}`;
      room.textContent = `Бежать в ауд. ${nextLesson.room || '—'}`;
      teacher.textContent = `👤 ${nextLesson.teacher || ''}`;
      bar.style.width = '100%';
      pill.style.background = 'rgba(245, 158, 11, 0.15)';
      pill.style.color = 'var(--accent-amber)';
    } else if (nextLesson) {
      const startMin = parseTimeToMinutes(nextLesson.time.split('-')[0]);
      const remain = startMin - currentMinutes;
      statusText.textContent = 'Скоро начнется';
      countdown.textContent = `Через ${remain} мин`;
      title.textContent = nextLesson.subject;
      room.textContent = `🚪 Ауд. ${nextLesson.room || '—'}`;
      teacher.textContent = `👤 ${nextLesson.teacher || ''}`;
      bar.style.width = '0%';
    }
  }

  function parseTimeToMinutes(timeStr) {
    if (!timeStr) return 0;
    const [h, m] = timeStr.trim().split(':').map(Number);
    return (h || 0) * 60 + (m || 0);
  }

  // --- Модальное окно выбора группы ---
  function renderGroupModalList() {
    const listEl = document.getElementById('group-select-list');
    if (!listEl) return;
    listEl.innerHTML = '';

    let groups = [];
    if (scheduleDb && Array.isArray(scheduleDb.groups)) {
      groups = scheduleDb.groups;
    } else {
      groups = [
        { name: '1-ИС-1', specialty: 'Информационные системы', course: 1 },
        { name: '1-КСД-1', specialty: 'Сетевое администрирование', course: 1 },
        { name: '1-КПД-2', specialty: 'Программирование', course: 1 },
        { name: '2-ИС-1', specialty: 'Информационные системы', course: 2 },
        { name: '4-КРД-36', specialty: 'Радиотехника', course: 4 }
      ];
    }

    const q = (state.groupSearchQuery || '').toLowerCase().trim();
    const cFilter = state.selectedCourseFilter;

    const filtered = groups.filter(g => {
      if (cFilter > 0 && g.course !== cFilter) return false;
      if (q && !g.name.toLowerCase().includes(q) && !(g.specialty || '').toLowerCase().includes(q)) {
        return false;
      }
      return true;
    });

    if (filtered.length === 0) {
      listEl.innerHTML = '<div style="grid-column: 1 / -1; text-align: center; color: var(--text-muted); padding: 20px;">Группа не найдена</div>';
      return;
    }

    filtered.forEach(grp => {
      const btn = document.createElement('button');
      btn.className = `group-select-btn ${grp.name === state.groupName ? 'active' : ''}`;
      btn.textContent = grp.name;
      btn.addEventListener('click', () => {
        switchGroup(grp.name);
        closeGroupModal();
      });
      listEl.appendChild(btn);
    });
  }

  function openGroupModal() {
    triggerHaptic('light');
    renderGroupModalList();
    const modal = document.getElementById('group-modal');
    if (modal) modal.style.display = 'flex';
  }

  function closeGroupModal() {
    const modal = document.getElementById('group-modal');
    if (modal) modal.style.display = 'none';
  }

  // --- Модальное окно выбора учебной недели ---
  function renderWeekModalList() {
    const listEl = document.getElementById('week-select-list');
    if (!listEl) return;
    listEl.innerHTML = '';

    const maxWeek = scheduleDb?.config?.maxWeek || 16;
    const currentWeekInfo = calculateAcademicWeekForDate(new Date());

    for (let w = 1; w <= maxWeek; w++) {
      const btn = document.createElement('button');
      const isSelected = (w === state.weekNumber);
      const isCurrent = (w === currentWeekInfo.weekNumber);
      btn.className = `week-select-btn${isSelected ? ' active' : ''}${isCurrent ? ' current-week' : ''}`;

      // Вычисляем даты этой недели
      const offset = w - currentWeekInfo.weekNumber;
      const mon = getMondayForOffset(offset);
      const sat = new Date(mon);
      sat.setDate(mon.getDate() + 5);

      const sDay = mon.getDate();
      const sMonth = MONTHS_GENITIVE_SHORT[mon.getMonth()];
      const eDay = sat.getDate();
      const eMonth = MONTHS_GENITIVE_SHORT[sat.getMonth()];

      const overrides = scheduleDb?.config?.weekParityOverrides || {};
      const parity = overrides[String(w)] ? (overrides[String(w)] === 'even' ? 'чётная' : 'нечётная') : (w % 2 === 0 ? 'чётная' : 'нечётная');

      btn.innerHTML = `
        <div class="week-btn-top">
          <span>Неделя ${w}</span>
          ${isCurrent ? '<span style="color: var(--accent-cyan); font-size: 11px;">Сегодня</span>' : ''}
        </div>
        <div class="week-btn-sub">${sDay} ${sMonth} – ${eDay} ${eMonth} • ${parity}</div>
      `;

      btn.addEventListener('click', () => {
        jumpToSpecificWeek(w);
      });
      listEl.appendChild(btn);
    }
  }

  function openWeekModal() {
    triggerHaptic('light');
    renderWeekModalList();
    const modal = document.getElementById('week-modal');
    if (modal) modal.style.display = 'flex';
  }

  function closeWeekModal() {
    const modal = document.getElementById('week-modal');
    if (modal) modal.style.display = 'none';
  }

  // --- Модальное окно расписания звонков ---
  function renderBellsList() {
    const listEl = document.getElementById('bells-list');
    if (!listEl) return;
    listEl.innerHTML = '';

    const isVoz = (state.selectedCorpusTab === 'voz');
    const bells = isVoz ? DEFAULT_BELLS_VOZ : DEFAULT_BELLS_MAIN;

    const now = new Date();
    const currentMin = now.getHours() * 60 + now.getMinutes();

    bells.forEach(b => {
      const row = document.createElement('div');
      const startMin = parseTimeToMinutes(b.start);
      const endMin = parseTimeToMinutes(b.end);
      const isActive = (currentMin >= startMin && currentMin < endMin);
      row.className = `bell-row${isActive ? ' active-bell' : ''}`;

      row.innerHTML = `
        <div class="bell-pair-label">
          <span class="pair-number">${b.num}</span>
          <span>${b.num} пара</span>
          ${isActive ? '<span style="color: var(--accent-emerald); font-size: 11px;">(идёт сейчас)</span>' : ''}
        </div>
        <div class="bell-pair-time">${b.start} – ${b.end}</div>
      `;
      listEl.appendChild(row);
    });
  }

  function openBellsModal() {
    triggerHaptic('light');
    renderBellsList();
    const modal = document.getElementById('bells-modal');
    if (modal) modal.style.display = 'flex';
  }

  function closeBellsModal() {
    const modal = document.getElementById('bells-modal');
    if (modal) modal.style.display = 'none';
  }

  // --- Настройка свайпов ---
  function setupSwipeGestures() {
    const scheduleArea = document.getElementById('lessons-timeline');
    const daysNavWrapper = document.querySelector('.days-nav-wrapper');
    let startX = 0;
    let startY = 0;
    let startTime = 0;

    function onTouchStart(e) {
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
      startTime = Date.now();
    }

    function onTouchEnd(e) {
      if (e.target && e.target.closest('button')) return;

      const dx = e.changedTouches[0].clientX - startX;
      const dy = e.changedTouches[0].clientY - startY;
      const dt = Date.now() - startTime;

      if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.8 && dt < 500) {
        if (dx < 0) {
          // Свайп влево: следующий день или следующая неделя
          if (state.selectedDayIndex < state.weekDays.length - 1) {
            triggerHaptic('selection');
            state.selectedDayIndex++;
            renderDaysNav();
            renderSchedule();
            updateLiveWidget();
            updateTodayJumpButton();
          } else {
            // Переход на понедельник следующей недели
            triggerHaptic('light');
            state.selectedDayIndex = 0;
            changeWeek(+1);
          }
        } else if (dx > 0) {
          // Свайп вправо: предыдущий день или суббота предыдущей недели
          if (state.selectedDayIndex > 0) {
            triggerHaptic('selection');
            state.selectedDayIndex--;
            renderDaysNav();
            renderSchedule();
            updateLiveWidget();
            updateTodayJumpButton();
          } else {
            // Переход на субботу предыдущей недели
            triggerHaptic('light');
            state.selectedDayIndex = 5;
            changeWeek(-1);
          }
        }
      }
    }

    if (scheduleArea) {
      scheduleArea.addEventListener('touchstart', onTouchStart, { passive: true });
      scheduleArea.addEventListener('touchend', onTouchEnd, { passive: true });
    }
    if (daysNavWrapper) {
      daysNavWrapper.addEventListener('touchstart', onTouchStart, { passive: true });
      daysNavWrapper.addEventListener('touchend', onTouchEnd, { passive: true });
    }
  }

  // --- Обработчики событий ---
  function setupEventListeners() {
    // Поиск
    const searchInput = document.getElementById('search-input');
    const searchClear = document.getElementById('search-clear');
    if (searchInput) {
      searchInput.addEventListener('input', (e) => {
        state.searchQuery = e.target.value.trim();
        if (searchClear) searchClear.style.display = state.searchQuery ? 'block' : 'none';
        renderSchedule();
      });
    }
    if (searchClear) {
      searchClear.addEventListener('click', () => {
        if (searchInput) searchInput.value = '';
        state.searchQuery = '';
        searchClear.style.display = 'none';
        renderSchedule();
      });
    }

    // Переключение тем
    const themeBtn = document.getElementById('theme-toggle');
    if (themeBtn) {
      themeBtn.addEventListener('click', () => {
        triggerHaptic('light');
        const isLight = document.body.classList.toggle('light-theme');
        const icon = document.querySelector('.theme-icon');
        if (icon) icon.textContent = isLight ? '☀️' : '🌙';
        showToast(isLight ? 'Светлая тема включена' : 'Тёмная тема включена');
      });
    }

    // Кнопки недель
    const prevBtn = document.getElementById('prev-week-btn');
    if (prevBtn) prevBtn.addEventListener('click', () => changeWeek(-1));

    const nextBtn = document.getElementById('next-week-btn');
    if (nextBtn) nextBtn.addEventListener('click', () => changeWeek(+1));

    const todayBtn = document.getElementById('btn-go-today');
    if (todayBtn) todayBtn.addEventListener('click', jumpToCurrentWeek);

    const weekTrigger = document.getElementById('week-picker-trigger');
    if (weekTrigger) weekTrigger.addEventListener('click', openWeekModal);

    const closeWeekBtn = document.getElementById('close-week-modal');
    if (closeWeekBtn) closeWeekBtn.addEventListener('click', closeWeekModal);

    const weekModalOverlay = document.getElementById('week-modal');
    if (weekModalOverlay) {
      weekModalOverlay.addEventListener('click', (e) => {
        if (e.target === weekModalOverlay) closeWeekModal();
      });
    }

    // Модальное окно звонков
    const bellsToggle = document.getElementById('bells-toggle');
    if (bellsToggle) bellsToggle.addEventListener('click', openBellsModal);

    const closeBellsBtn = document.getElementById('close-bells-modal');
    if (closeBellsBtn) closeBellsBtn.addEventListener('click', closeBellsModal);

    const bellsModalOverlay = document.getElementById('bells-modal');
    if (bellsModalOverlay) {
      bellsModalOverlay.addEventListener('click', (e) => {
        if (e.target === bellsModalOverlay) closeBellsModal();
      });
    }

    document.querySelectorAll('.bells-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.bells-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        state.selectedCorpusTab = tab.dataset.corpus || 'main';
        renderBellsList();
      });
    });

    // Модальное окно групп
    const groupTrigger = document.getElementById('group-switcher-trigger');
    if (groupTrigger) groupTrigger.addEventListener('click', openGroupModal);

    const closeGroupBtn = document.getElementById('close-group-modal');
    if (closeGroupBtn) closeGroupBtn.addEventListener('click', closeGroupModal);

    const groupModalOverlay = document.getElementById('group-modal');
    if (groupModalOverlay) {
      groupModalOverlay.addEventListener('click', (e) => {
        if (e.target === groupModalOverlay) closeGroupModal();
      });
    }

    const groupSearchInput = document.getElementById('group-search-input');
    if (groupSearchInput) {
      groupSearchInput.addEventListener('input', (e) => {
        state.groupSearchQuery = e.target.value;
        renderGroupModalList();
      });
    }

    document.querySelectorAll('.course-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.course-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        state.selectedCourseFilter = parseInt(tab.dataset.course, 10) || 0;
        renderGroupModalList();
      });
    });

    // Кнопка «Поделиться»
    const shareBtn = document.getElementById('btn-share');
    if (shareBtn) {
      shareBtn.addEventListener('click', () => {
        triggerHaptic('light');
        const group = state.groupName || '1-ИС-1';
        const cleanUrl = getCleanShareUrl();
        const shareText = `Расписание занятий группы ${group} в КТМУ`;

        if (window.Telegram?.WebApp?.openTelegramLink) {
          const tmeUrl = `https://t.me/share/url?url=${encodeURIComponent(cleanUrl)}&text=${encodeURIComponent(shareText)}`;
          try {
            window.Telegram.WebApp.openTelegramLink(tmeUrl);
            return;
          } catch (err) {}
        }

        if (navigator.share) {
          navigator.share({ title: `Расписание ${group}`, text: shareText, url: cleanUrl }).catch(() => {});
          return;
        }

        if (navigator.clipboard?.writeText) {
          navigator.clipboard.writeText(`${shareText}\n${cleanUrl}`);
          showToast('Ссылка скопирована в буфер обмена 📋');
        } else {
          showToast('Ссылка: ' + cleanUrl);
        }
      });
    }

    // Кнопка «Обновить»
    const refreshBtn = document.getElementById('btn-refresh');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        triggerHaptic('success');
        refreshWeekSchedule();
        renderHeader();
        renderDaysNav();
        renderSchedule();
        updateLiveWidget();
        showToast('Расписание обновлено 🔄');
      });
    }

    // Подгруппы
    document.querySelectorAll('.subgroup-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const sub = parseInt(btn.dataset.sub, 10) || 0;
        setSubgroup(sub);
      });
    });

    const subBadge = document.getElementById('subgroup-badge');
    if (subBadge) {
      subBadge.addEventListener('click', () => {
        triggerHaptic('light');
        const wrapper = document.getElementById('subgroup-notice-wrapper');
        if (wrapper) {
          wrapper.style.display = wrapper.style.display === 'none' ? 'block' : 'none';
        }
      });
    }
  }

  // --- Инициализация ---
  async function init() {
    initUserSession();
    await loadScheduleDb();
    loadScheduleData();
    state.selectedDayIndex = determineDefaultDayIndex();
    renderHeader();
    renderSubgroupUI();
    renderDaysNav();
    renderSchedule();
    updateLiveWidget();
    updateTodayJumpButton();
    setupEventListeners();
    setupSwipeGestures();

    state.timerInterval = setInterval(updateLiveWidget, 30000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
