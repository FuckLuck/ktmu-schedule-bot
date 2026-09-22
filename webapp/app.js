/**
 * Расписание КТМУ — Telegram Mini App (WebApp)
 * Клиентская логика, парсинг данных, Live-виджет и тактильный отклик.
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
    // Применяем цвета Telegram клиента
    if (tg.colorScheme === 'light') {
      document.body.classList.add('light-theme');
    }
  }

  // --- Состояние приложения ---
  const state = {
    userId: null,
    groupName: '1-КСД-1',
    weekNumber: 4,
    isEvenWeek: false,
    subgroup: 0, // 0 = Все, 1 = 1-я подгруппа, 2 = 2-я подгруппа
    subgroupChosen: false, // труе если пользователь явно выбрал подгруппу
    selectedDayIndex: 0, // 0 = Пн, 1 = Вт, ...
    searchQuery: '',
    weekDays: [],
    skippedPairs: new Set(), // Набор "YYYY-MM-DD:pairNum"
    monthlySkippedTotal: 0,
    timerInterval: null
  };

  const RU_WEEKDAYS = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота'];
  const RU_WEEKDAYS_SHORT = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'];
  const MONTHS_RU = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];

  // Расписание звонков КТМУ по умолчанию
  const DEFAULT_BELLS = [
    { num: 1, start: '08:30', end: '10:00' },
    { num: 2, start: '10:10', end: '11:40' },
    { num: 3, start: '11:50', end: '13:20' },
    { num: 4, start: '14:00', end: '15:30' },
    { num: 5, start: '15:40', end: '17:10' },
    { num: 6, start: '17:20', end: '18:50' },
    { num: 7, start: '19:00', end: '20:30' }
  ];

  // --- Вспомогательные функции тактильного отклика (Haptics) ---
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

  // --- Показ Toast сообщений ---
  function showToast(text) {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = text;
    toast.classList.add('show');
    setTimeout(() => {
      toast.classList.remove('show');
    }, 2400);
  }

  // --- Обновление адресной строки и генерация красивой ссылки ---
  function updateAddressBar() {
    try {
      const group = state.groupName || '1-КСД-1';
      let search = `?group=${encodeURIComponent(group)}`;
      if (state.subgroup && state.subgroup > 0) {
        search += `&subgroup=${state.subgroup}`;
      }
      if (window.history && window.history.replaceState) {
        window.history.replaceState(null, '', window.location.pathname + search);
      }
    } catch (e) {}
  }

  function getCleanShareUrl() {
    const origin = window.location.origin || 'https://ktmu-schedule-bot-plqd-one.vercel.app';
    const group = state.groupName || '1-КСД-1';
    let url = `${origin}/?group=${encodeURIComponent(group)}`;
    if (state.subgroup && state.subgroup > 0) {
      url += `&subgroup=${state.subgroup}`;
    }
    return url;
  }

  // --- Загрузка и декодирование расписания ---
  function loadScheduleData() {
    // 1. Попытка чтения из URL hash (#data=...)
    const hash = window.location.hash;
    if (hash.includes('data=')) {
      try {
        const rawData = hash.split('data=')[1].split('&')[0];
        const jsonStr = decodeURIComponent(escape(atob(rawData.replace(/-/g, '+').replace(/_/g, '/'))));
        const parsed = JSON.parse(jsonStr);
        applyParsedData(parsed);
        localStorage.setItem('ktmu_schedule_cache', jsonStr);

        // Убираем гигантский hash (#data=...) из адресной строки, заменяя на красивый URL: /?group=...
        updateAddressBar();
        return;
      } catch (e) {
        console.warn('Не удалось распарсить hash данные:', e);
      }
    }

    // 2. Чтение из URL параметров (?group=...&subgroup=...)
    const urlParams = new URLSearchParams(window.location.search);
    const queryGroup = urlParams.get('group');
    const querySubgroup = urlParams.get('subgroup');
    if (queryGroup) {
      state.groupName = queryGroup;
      try {
        localStorage.setItem('ktmu_selected_group', queryGroup);
      } catch (e) {}
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
      // Проверяем кэш для этой группы
      const cached = localStorage.getItem('ktmu_schedule_cache');
      if (cached) {
        try {
          const parsed = JSON.parse(cached);
          if (parsed && (parsed.group_name === queryGroup || parsed.group === queryGroup)) {
            applyParsedData(parsed);
            return;
          }
        } catch (e) {}
      }
      applyMockData(queryGroup);
      return;
    }

    // 3. Попытка чтения из localStorage
    const savedGroup = localStorage.getItem('ktmu_selected_group') || '1-КСД-1';
    const cached = localStorage.getItem('ktmu_schedule_cache');
    if (cached) {
      try {
        const parsed = JSON.parse(cached);
        if (parsed && (parsed.group_name === savedGroup || parsed.group === savedGroup)) {
          applyParsedData(parsed);
          return;
        }
      } catch (e) {
        console.warn('Ошибка кэша:', e);
      }
    }

    // 4. Fallback реальное расписание выбранной группы
    applyMockData(savedGroup);
  }

  function applyParsedData(data) {
    state.groupName = data.group_name || data.group || localStorage.getItem('ktmu_selected_group') || '1-КСД-1';
    state.weekNumber = data.week_number || 4;
    state.isEvenWeek = !!data.is_even;
    state.weekDays = data.days || [];
    if (data.subgroup !== undefined && data.subgroup !== null) {
      state.subgroup = parseInt(data.subgroup, 10) || 0;
      try {
        localStorage.setItem('ktmu_user_subgroup', String(state.subgroup));
      } catch (e) {}
    }
    // Бот говорит: пользователь уже явно выбрал подгруппу
    if (data.subgroup_chosen) {
      state.subgroupChosen = true;
      try {
        localStorage.setItem('ktmu_subgroup_chosen', '1');
      } catch (e) {}
    }
    try {
      localStorage.setItem('ktmu_selected_group', state.groupName);
      localStorage.setItem('ktmu_schedule_cache', JSON.stringify(data));
    } catch (e) {}
    if (data.user_id) {
      state.userId = parseInt(data.user_id, 10);
      try {
        localStorage.setItem('ktmu_user_id', state.userId);
      } catch (e) {}
    }
    if (Array.isArray(data.skipped_pairs)) {
      data.skipped_pairs.forEach(k => state.skippedPairs.add(k));
      saveLocalSkips();
    }
  }

  // --- Синхронизация пользователя и пропусков пар с ботом ---
  function initUserSession() {
    // 1. Telegram WebApp ID
    if (tg?.initDataUnsafe?.user?.id) {
      state.userId = tg.initDataUnsafe.user.id;
    }
    // 2. URL параметр ?user_id=...
    if (!state.userId) {
      const params = new URLSearchParams(window.location.search);
      const uid = params.get('user_id');
      if (uid && !isNaN(parseInt(uid, 10))) {
        state.userId = parseInt(uid, 10);
      }
    }
    // 3. Локальный кэш пользователя
    if (!state.userId) {
      const saved = localStorage.getItem('ktmu_user_id');
      if (saved) state.userId = parseInt(saved, 10);
    } else {
      try {
        localStorage.setItem('ktmu_user_id', state.userId);
      } catch (e) {}
    }

    // 4. Загрузка подгруппы
    const savedSub = localStorage.getItem('ktmu_user_subgroup');
    if (savedSub !== null && !isNaN(parseInt(savedSub, 10))) {
      state.subgroup = parseInt(savedSub, 10);
    }

    // 5. Проверяем флаг "subgroup_chosen" (бот передал через URL или localStorage)
    const chosenFromStorage = localStorage.getItem('ktmu_subgroup_chosen');
    if (chosenFromStorage === '1') {
      state.subgroupChosen = true;
    }
    // URL параметр subgroup_chosen=1
    const params2 = new URLSearchParams(window.location.search);
    if (params2.get('subgroup_chosen') === '1') {
      state.subgroupChosen = true;
      try { localStorage.setItem('ktmu_subgroup_chosen', '1'); } catch (e) {}
    }

    // 6. Загрузка локальных пропусков
    const local = localStorage.getItem('ktmu_skipped_pairs');
    if (local) {
      try {
        const arr = JSON.parse(local);
        if (Array.isArray(arr)) {
          arr.forEach(k => state.skippedPairs.add(k));
        }
      } catch (e) {}
    }

    // 7. Подсчет пропусков за текущий месяц
    const currentMonth = new Date().toISOString().slice(0, 7);
    let mCount = 0;
    state.skippedPairs.forEach(k => {
      if (k.startsWith(currentMonth)) mCount++;
    });
    state.monthlySkippedTotal = mCount;

    syncSkipsFromServer();
  }

  function syncSkipsFromServer() {
    // Пропуски загружаются из URL-данных бота (data=...) при открытии мини-апп.
    // Дополнительный fetch не нужен — Vercel не имеет доступа к боту.
    // Данные уже применены в applyParsedData() при loadScheduleData().
    const currentMonth = new Date().toISOString().slice(0, 7);
    let mCount = 0;
    state.skippedPairs.forEach(k => {
      if (k.startsWith(currentMonth)) mCount++;
    });
    state.monthlySkippedTotal = mCount;
  }

  function saveLocalSkips() {
    try {
      localStorage.setItem('ktmu_skipped_pairs', JSON.stringify(Array.from(state.skippedPairs)));
    } catch (e) {}
  }

  const REAL_SCHEDULES = {
    '1-КСД-1': [
      // Пн (день 0)
      [
        {
          pair_number: 2,
          time: '10:10-11:40',
          subject: 'Правовое и документ. обеспечение управления страховой орг.',
          lesson_type: 'Практика',
          room: '21',
          teacher: 'Круглов И.В.',
          subgroup: 0
        },
        {
          pair_number: 3,
          time: '11:50-13:20',
          subject: 'Основы экономической теории',
          lesson_type: 'Практика',
          room: '51',
          teacher: 'Пономарченко А.Е.',
          subgroup: 0
        }
      ],
      // Вт (день 1)
      [
        {
          pair_number: 3,
          time: '11:50-13:20',
          subject: 'Математика',
          lesson_type: 'Лекция',
          room: 'Дистант',
          teacher: 'Братищева В.А.',
          subgroup: 0
        },
        {
          pair_number: 4,
          time: '14:00-15:30',
          subject: 'Психология общения',
          lesson_type: 'Лекция',
          room: 'Дистант',
          teacher: 'Гречканева А.Г.',
          subgroup: 0
        },
        {
          pair_number: 5,
          time: '15:40-17:10',
          subject: 'Иностранный язык в проф. деятельности',
          lesson_type: 'Практика',
          room: 'Дистант',
          teacher: 'Сорваль М.П.',
          subgroup: 0
        },
        {
          pair_number: 6,
          time: '17:20-18:50',
          subject: 'Безопасность жизнедеятельности',
          lesson_type: 'Практика',
          room: 'Дистант',
          teacher: 'Михайлов И.К.',
          subgroup: 0
        }
      ],
      // Ср (день 2)
      [
        {
          pair_number: 2,
          time: '10:10-11:40',
          subject: 'Физическая культура',
          lesson_type: 'Практика',
          room: 'Спортзал',
          teacher: 'Кузнецов Д.М.',
          subgroup: 0
        },
        {
          pair_number: 3,
          time: '11:50-13:20',
          subject: 'Физическая культура',
          lesson_type: 'Практика',
          room: 'Спортзал',
          teacher: 'Кузнецов Д.М.',
          subgroup: 0
        }
      ],
      // Чт (день 3)
      [
        {
          pair_number: 2,
          time: '10:10-11:40',
          subject: 'Основы финансовой грамотности',
          lesson_type: 'Практика',
          room: '38',
          teacher: 'Соколова Е.Н.',
          subgroup: 0
        },
        {
          pair_number: 3,
          time: '11:50-13:20',
          subject: 'Страховое дело',
          lesson_type: 'Практика',
          room: '39',
          teacher: 'Круглов И.В.',
          subgroup: 0
        },
        {
          pair_number: 4,
          time: '14:00-15:30',
          subject: 'Математика',
          lesson_type: 'Практика',
          room: '3',
          teacher: 'Братищева В.А.',
          subgroup: 0
        }
      ],
      // Пт (день 4)
      [
        {
          pair_number: 5,
          time: '15:40-17:10',
          subject: 'Информационные технологии в проф. деят.',
          lesson_type: 'Практика',
          room: 'Дистант',
          teacher: 'Федорова О.С.',
          subgroup: 0
        },
        {
          pair_number: 6,
          time: '17:20-18:50',
          subject: 'Информационные технологии в проф. деят.',
          lesson_type: 'Лаб',
          room: 'Дистант',
          teacher: 'Федорова О.С.',
          subgroup: 0
        }
      ],
      // Сб (день 5)
      [
        {
          pair_number: 3,
          time: '11:50-13:20',
          subject: 'История',
          lesson_type: 'Лекция',
          room: 'Дистант',
          teacher: 'Иванова Е.В.',
          subgroup: 0
        },
        {
          pair_number: 4,
          time: '14:00-15:30',
          subject: 'Русский язык',
          lesson_type: 'Практика',
          room: 'Дистант',
          teacher: 'Ковалева Н.С.',
          subgroup: 0
        }
      ]
    ],
    '1-КПД-2': [
      // Пн (день 0)
      [
        {
          pair_number: 2,
          time: '10:10-11:40',
          subject: 'Математика',
          lesson_type: 'Практика',
          room: '39',
          teacher: 'Братищева В.А.',
          subgroup: 0
        },
        {
          pair_number: 3,
          time: '11:50-13:20',
          subject: 'Математика',
          lesson_type: 'Лекция',
          room: '39',
          teacher: 'Братищева В.А.',
          subgroup: 0
        },
        {
          pair_number: 4,
          time: '14:00-15:30',
          subject: 'Иностранный язык',
          lesson_type: 'Практика',
          room: '204',
          teacher: 'Сорваль М.П.',
          subgroup: 2
        }
      ],
      // Вт (день 1)
      [
        {
          pair_number: 1,
          time: '08:30-10:00',
          subject: 'Математика',
          lesson_type: 'Практика',
          room: '3',
          teacher: 'Братищева В.А.',
          subgroup: 0
        },
        {
          pair_number: 2,
          time: '10:10-11:40',
          subject: 'История',
          lesson_type: 'Практика',
          room: '45',
          teacher: 'Иванова Е.В.',
          subgroup: 0
        },
        {
          pair_number: 3,
          time: '11:50-13:20',
          subject: 'Русский язык',
          lesson_type: 'Практика',
          room: '31',
          teacher: 'Ковалева Н.С.',
          subgroup: 0
        },
        {
          pair_number: 4,
          time: '14:00-15:30',
          subject: 'Физика',
          lesson_type: 'Практика',
          room: '50',
          teacher: 'Семенов А.П.',
          subgroup: 0
        }
      ],
      // Ср (день 2)
      [
        {
          pair_number: 1,
          time: '08:30-10:00',
          subject: 'Обществознание',
          lesson_type: 'Практика',
          room: '5',
          teacher: 'Николаев В.Г.',
          subgroup: 0
        },
        {
          pair_number: 2,
          time: '10:10-11:40',
          subject: 'Физическая культура',
          lesson_type: 'Практика',
          room: 'Спортзал',
          teacher: 'Кузнецов Д.М.',
          subgroup: 0
        },
        {
          pair_number: 3,
          time: '11:50-13:20',
          subject: 'Информатика и ИКТ',
          lesson_type: 'Практика',
          room: '40',
          teacher: 'Федорова О.С.',
          subgroup: 1
        }
      ],
      // Чт (день 3)
      [
        {
          pair_number: 2,
          time: '10:05-11:30',
          subject: 'Введение в специальность',
          lesson_type: 'Практика',
          room: 'Вознесенский пр., 44',
          teacher: 'Михайлов И.К.',
          subgroup: 0,
          is_external: true
        },
        {
          pair_number: 3,
          time: '11:40-13:05',
          subject: 'Введение в специальность',
          lesson_type: 'Практика',
          room: 'Вознесенский пр., 44',
          teacher: 'Михайлов И.К.',
          subgroup: 0,
          is_external: true
        }
      ],
      // Пт (день 4)
      [
        {
          pair_number: 2,
          time: '10:10-11:40',
          subject: 'Литература',
          lesson_type: 'Практика',
          room: '44',
          teacher: 'Ковалева Н.С.',
          subgroup: 0
        },
        {
          pair_number: 3,
          time: '11:50-13:20',
          subject: 'Литература',
          lesson_type: 'Практика',
          room: '30',
          teacher: 'Ковалева Н.С.',
          subgroup: 0
        },
        {
          pair_number: 4,
          time: '14:00-15:30',
          subject: 'География',
          lesson_type: 'Практика',
          room: '37',
          teacher: 'Попова Т.А.',
          subgroup: 0
        }
      ],
      // Сб (день 5)
      []
    ]
  };

  function applyMockData(groupName) {
    const targetGroup = groupName || state.groupName || localStorage.getItem('ktmu_selected_group') || '1-КСД-1';
    state.groupName = targetGroup;
    state.weekNumber = 4;
    state.isEvenWeek = true;

    // Генерируем учебную неделю от текущего понедельника
    const now = new Date();
    const dayOfWeek = (now.getDay() + 6) % 7; // 0 = Пн, 6 = Вс
    const monday = new Date(now);
    monday.setDate(now.getDate() - dayOfWeek);

    const lessonsByDay = REAL_SCHEDULES[targetGroup] || REAL_SCHEDULES['1-КСД-1'];

    const days = [];
    for (let i = 0; i < 6; i++) {
      const d = new Date(monday);
      d.setDate(monday.getDate() + i);
      const isoDate = d.toISOString().split('T')[0];
      days.push({
        date: isoDate,
        day_name: RU_WEEKDAYS[i],
        lessons: lessonsByDay[i] || []
      });
    }
    state.weekDays = days;
  }

  function switchGroup(groupName) {
    applyMockData(groupName);
    try {
      localStorage.setItem('ktmu_selected_group', groupName);
      localStorage.removeItem('ktmu_schedule_cache');
    } catch (e) {}
    updateAddressBar();
    renderHeader();
    renderDaysNav();
    renderSchedule();
    updateLiveWidget();
    renderGroupModalList();
    showToast(`Выбрана группа ${groupName} ✅`);
  }

  function renderGroupModalList() {
    const listEl = document.getElementById('group-select-list');
    if (!listEl) return;
    const availableGroups = ['1-КСД-1', '1-КПД-2', '1-КИД-3', '4-КРД-36'];
    listEl.innerHTML = '';
    availableGroups.forEach(grp => {
      const btn = document.createElement('button');
      btn.className = `group-select-btn ${grp === state.groupName ? 'active' : ''}`;
      btn.textContent = grp;
      btn.onclick = () => {
        switchGroup(grp);
        closeGroupModal();
      };
      listEl.appendChild(btn);
    });
  }

  function openGroupModal() {
    renderGroupModalList();
    const modal = document.getElementById('group-modal');
    if (modal) modal.style.display = 'flex';
  }

  function closeGroupModal() {
    const modal = document.getElementById('group-modal');
    if (modal) modal.style.display = 'none';
  }

  // --- Определение активного дня по умолчанию ---
  function determineDefaultDayIndex() {
    const now = new Date();
    const dayOfWeek = (now.getDay() + 6) % 7; // 0 = Пн, 6 = Вс
    if (dayOfWeek >= 0 && dayOfWeek < 6) {
      return dayOfWeek;
    }
    return 0; // В воскресенье показываем понедельник
  }

  // --- Отрисовка шапки ---
  function renderHeader() {
    document.getElementById('group-title').textContent = state.groupName;
    const parityText = state.isEvenWeek ? 'чётная' : 'нечётная';
    document.getElementById('week-badge').textContent = `Неделя ${state.weekNumber} • ${parityText}`;
  }

  // --- Отрисовка подгруппы и плашки ---
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

    // Плашка скрывается если:
    // 1) state.subgroupChosen = true (пользователь явно выбрал через мини-апп или бот передал subgroup_chosen=true)
    // 2) ktmu_subgroup_chosen === '1' в localStorage
    const hasChosen = state.subgroupChosen || localStorage.getItem('ktmu_subgroup_chosen') === '1';
    if (wrapper) {
      wrapper.style.display = hasChosen ? 'none' : 'block';
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
    // Подгруппа сохраняется локально; бот получит обновление при следующем открытии через /start
  }

  // --- Отрисовка ленты дней (Пн-Сб) ---
  function renderDaysNav() {
    const nav = document.getElementById('days-nav');
    nav.innerHTML = '';

    const todayIso = new Date().toISOString().split('T')[0];

    state.weekDays.forEach((dayData, idx) => {
      const pill = document.createElement('div');
      pill.className = `day-pill ${idx === state.selectedDayIndex ? 'active' : ''}`;
      if (dayData.date === todayIso) {
        pill.classList.add('today');
      }

      // Форматируем дату (число + краткий месяц)
      let dateDisplay = '';
      if (dayData.date) {
        const parts = dayData.date.split('-');
        if (parts.length === 3) {
          dateDisplay = `${parseInt(parts[2], 10)} ${MONTHS_RU[parseInt(parts[1], 10) - 1].slice(0, 3)}`;
        }
      }

      // Подсчет количества пар с учетом подгруппы
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
      });

      nav.appendChild(pill);
    });

    // Скролл к выбранному дню
    const activePill = nav.querySelector('.day-pill.active');
    if (activePill) {
      activePill.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
    }
  }

  // --- Фильтрация занятий ---
  function getFilteredLessons(lessons) {
    if (!lessons) return [];
    return lessons.filter(l => {
      // Фильтр по подгруппе
      if (state.subgroup === 1 && l.subgroup === 2) return false;
      if (state.subgroup === 2 && l.subgroup === 1) return false;

      // Фильтр по поисковому запросу
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

  // --- Отрисовка карточек расписания ---
  function renderSchedule() {
    const dayData = state.weekDays[state.selectedDayIndex];
    if (!dayData) return;

    // Обновляем заголовок дня
    document.getElementById('current-day-label').textContent = dayData.day_name || RU_WEEKDAYS[state.selectedDayIndex];
    if (dayData.date) {
      const parts = dayData.date.split('-');
      if (parts.length === 3) {
        document.getElementById('current-day-date').textContent = `${parseInt(parts[2], 10)} ${MONTHS_RU[parseInt(parts[1], 10) - 1]}`;
      }
    }

    const container = document.getElementById('lessons-timeline');
    const emptyState = document.getElementById('empty-state');
    container.innerHTML = '';

    const filtered = getFilteredLessons(dayData.lessons);

    if (filtered.length === 0) {
      emptyState.style.display = 'flex';
      return;
    } else {
      emptyState.style.display = 'none';
    }

    filtered.forEach(lesson => {
      const card = createLessonCard(lesson, dayData.date);
      container.appendChild(card);
    });
  }

  // --- Создание карточки пары ---
  function createLessonCard(lesson, dateStr) {
    const pairNum = lesson.pair_number || 1;
    const skipKey = `${dateStr}:${pairNum}`;

    // Тип занятия
    let typeClass = '';
    const lType = (lesson.lesson_type || '').toLowerCase();
    if (lType.includes('лекц')) typeClass = 'type-lecture';
    else if (lType.includes('практ')) typeClass = 'type-practice';
    else if (lType.includes('лаб')) typeClass = 'type-lab';

    // Локация: Дистант / Вознесенский / обычная аудитория
    const room = lesson.room || 'Не указана';
    const roomLower = room.toLowerCase();
    const isDistant = roomLower.includes('дистант') || roomLower.includes('online') || roomLower.includes('онлайн');
    const isVozn = roomLower.includes('вознесен') || lesson.is_external;

    let roomBadge, roomClass;
    if (isDistant) {
      roomBadge = '🖥️ Дистант';
      roomClass = 'room-distant';
    } else if (isVozn) {
      roomBadge = '🏛️ Вознесенский пр.';
      roomClass = 'room-external';
    } else {
      roomBadge = `🚪 Ауд. ${room}`;
      roomClass = '';
    }

    // Создаём элементы напрямую (без innerHTML для кнопки — надёжнее)
    const card = document.createElement('div');

    function refreshCard() {
      const skipped = state.skippedPairs.has(skipKey);
      card.className = `lesson-card${skipped ? ' skipped' : ''}`;

      // lesson-top
      const top = document.createElement('div');
      top.className = 'lesson-top';
      const timeBadge = document.createElement('div');
      timeBadge.className = 'lesson-time-badge';
      const numSpan = document.createElement('span');
      numSpan.className = 'pair-number';
      numSpan.textContent = pairNum;
      const timeSpan = document.createElement('span');
      timeSpan.className = 'pair-time';
      timeSpan.textContent = lesson.time || '';
      timeBadge.appendChild(numSpan);
      timeBadge.appendChild(timeSpan);
      top.appendChild(timeBadge);
      if (lesson.lesson_type) {
        const typeBadge = document.createElement('span');
        typeBadge.className = `lesson-type-badge ${typeClass}`;
        typeBadge.textContent = lesson.lesson_type;
        top.appendChild(typeBadge);
      }

      // subject
      const subj = document.createElement('div');
      subj.className = 'lesson-subject';
      subj.textContent = lesson.subject || 'Учебное занятие';

      // details
      const details = document.createElement('div');
      details.className = 'lesson-details';
      const roomEl = document.createElement('span');
      roomEl.className = `detail-item room${roomClass ? ' ' + roomClass : ''}`;
      roomEl.textContent = roomBadge;
      details.appendChild(roomEl);
      if (lesson.teacher) {
        const teacherEl = document.createElement('span');
        teacherEl.className = 'detail-item';
        teacherEl.textContent = `👤 ${lesson.teacher}`;
        details.appendChild(teacherEl);
      }
      if (lesson.subgroup) {
        const sgEl = document.createElement('span');
        sgEl.className = 'detail-item subgroup';
        sgEl.textContent = `👥 ${lesson.subgroup} подгр.`;
        details.appendChild(sgEl);
      }

      card.innerHTML = '';
      card.appendChild(top);
      card.appendChild(subj);
      card.appendChild(details);
    }

    refreshCard();
    return card;
  }

  // --- Поддержка свайпа по ленте дней (только по навигации, не по карточкам) ---
  function setupSwipeGestures() {
    // Свайп только по области расписания, но НЕ по кнопкам
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
      // Игнорируем если начали на кнопке
      if (e.target && e.target.closest('button')) return;

      const dx = e.changedTouches[0].clientX - startX;
      const dy = e.changedTouches[0].clientY - startY;
      const dt = Date.now() - startTime;

      if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 2 && dt < 500) {
        if (dx < 0 && state.selectedDayIndex < state.weekDays.length - 1) {
          triggerHaptic('selection');
          state.selectedDayIndex++;
          animateDaySwitch('next');
        } else if (dx > 0 && state.selectedDayIndex > 0) {
          triggerHaptic('selection');
          state.selectedDayIndex--;
          animateDaySwitch('prev');
        }
      }
    }

    // Вешаем ТОЛЬКО на область расписания (не на кнопки!)
    if (scheduleArea) {
      scheduleArea.addEventListener('touchstart', onTouchStart, { passive: true });
      scheduleArea.addEventListener('touchend', onTouchEnd, { passive: true });
    }
    if (daysNavWrapper) {
      daysNavWrapper.addEventListener('touchstart', onTouchStart, { passive: true });
      daysNavWrapper.addEventListener('touchend', onTouchEnd, { passive: true });
    }

    // Клавиатура
    document.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowLeft' && state.selectedDayIndex > 0) {
        state.selectedDayIndex--;
        animateDaySwitch('prev');
      } else if (e.key === 'ArrowRight' && state.selectedDayIndex < state.weekDays.length - 1) {
        state.selectedDayIndex++;
        animateDaySwitch('next');
      }
    });
  }

  function animateDaySwitch(direction) {
    const timeline = document.getElementById('lessons-timeline');
    if (timeline) {
      timeline.style.opacity = '0.3';
      timeline.style.transform = direction === 'next' ? 'translateX(15px)' : 'translateX(-15px)';
      timeline.style.transition = 'all 0.15s ease-out';
      setTimeout(() => {
        renderDaysNav();
        renderSchedule();
        updateLiveWidget();
        timeline.style.opacity = '1';
        timeline.style.transform = 'translateX(0)';
      }, 100);
    } else {
      renderDaysNav();
      renderSchedule();
      updateLiveWidget();
    }
  }

  // --- Live-виджет текущей пары и отсчета времени ---
  function updateLiveWidget() {
    const widget = document.getElementById('live-widget');
    const todayIso = new Date().toISOString().split('T')[0];
    const dayData = state.weekDays[state.selectedDayIndex];

    // Виджет показывается только если просматривается СЕГОДНЯШНИЙ день
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

    // Определяем временные интервалы пар
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
        // Проверяем, идет ли сейчас перемена
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

  // --- Обработчики событий ---
  function setupEventListeners() {
    // Поиск
    const searchInput = document.getElementById('search-input');
    const searchClear = document.getElementById('search-clear');
    searchInput.addEventListener('input', (e) => {
      state.searchQuery = e.target.value.trim();
      searchClear.style.display = state.searchQuery ? 'block' : 'none';
      renderSchedule();
    });
    searchClear.addEventListener('click', () => {
      searchInput.value = '';
      state.searchQuery = '';
      searchClear.style.display = 'none';
      renderSchedule();
    });

    // Переключение темы (светлая/тёмная)
    document.getElementById('theme-toggle').addEventListener('click', () => {
      triggerHaptic('light');
      const isLight = document.body.classList.toggle('light-theme');
      const icon = document.querySelector('.theme-icon');
      if (icon) {
        icon.textContent = isLight ? '☀️' : '🌙';
      }
      showToast(isLight ? 'Светлая тема включена' : 'Тёмная тема включена');
    });

    // Кнопка «Поделиться»
    document.getElementById('btn-share').addEventListener('click', () => {
      triggerHaptic('light');

      const group = state.groupName || '1-КСД-1';
      const cleanUrl = getCleanShareUrl();
      const shareTitle = `Расписание ${group}`;
      const shareText = `Расписание занятий группы ${group} в КТМУ`;

      // 1. В Telegram WebApp открываем нативный диалог пересылки в чаты Telegram
      if (window.Telegram?.WebApp?.openTelegramLink) {
        const tmeUrl = `https://t.me/share/url?url=${encodeURIComponent(cleanUrl)}&text=${encodeURIComponent(shareText)}`;
        try {
          window.Telegram.WebApp.openTelegramLink(tmeUrl);
          return;
        } catch (err) {
          console.warn('Telegram openTelegramLink failed, trying fallback:', err);
        }
      }

      // 2. Стандартный Web Share API мобильных браузеров (Safari, Chrome и др.)
      if (navigator.share) {
        navigator.share({
          title: shareTitle,
          text: shareText,
          url: cleanUrl
        }).catch(() => {});
        return;
      }

      // 3. Fallback — копирование ссылки в буфер обмена
      if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(`${shareText}\n${cleanUrl}`);
        showToast('Ссылка скопирована в буфер обмена 📋');
      } else {
        showToast('Ссылка: ' + cleanUrl);
      }
    });

    // Кнопка «Обновить»
    document.getElementById('btn-refresh').addEventListener('click', () => {
      triggerHaptic('success');
      showToast('Расписание обновлено 🔄');
      renderSchedule();
      updateLiveWidget();
    });

    // Модальное окно выбора группы
    const switcherTrigger = document.getElementById('group-switcher-trigger');
    if (switcherTrigger) {
      switcherTrigger.addEventListener('click', () => {
        triggerHaptic('light');
        openGroupModal();
      });
    }

    const closeBtn = document.getElementById('close-group-modal');
    if (closeBtn) {
      closeBtn.addEventListener('click', closeGroupModal);
    }

    const modalOverlay = document.getElementById('group-modal');
    if (modalOverlay) {
      modalOverlay.addEventListener('click', (e) => {
        if (e.target === modalOverlay) closeGroupModal();
      });
    }

    // Кнопки выбора подгруппы в желтой плашке
    document.querySelectorAll('.subgroup-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const sub = parseInt(btn.dataset.sub, 10) || 0;
        setSubgroup(sub);
      });
    });

    // Клик по бейджу подгруппы в шапке
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

  // --- Инициализация приложения ---
  function init() {
    initUserSession();
    loadScheduleData();
    state.selectedDayIndex = determineDefaultDayIndex();
    renderHeader();
    renderSubgroupUI();
    renderDaysNav();
    renderSchedule();
    updateLiveWidget();
    setupEventListeners();
    setupSwipeGestures();

    // Запуск таймера Live-виджета
    state.timerInterval = setInterval(updateLiveWidget, 30000);
  }

  // Запуск при готовности DOM
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
