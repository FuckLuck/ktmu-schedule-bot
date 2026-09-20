/**
 * Расписание КТМУ — Telegram Mini App (WebApp)
 * Клиентская логика, парсинг данных, Live-виджет и тактильный отклик.
 */

(function () {
  'use strict';

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
    groupName: '1-КПД-2',
    weekNumber: 4,
    isEvenWeek: false,
    selectedSubgroup: 0, // 0 = Все, 1 = 1-я, 2 = 2-я
    selectedDayIndex: 0, // 0 = Пн, 1 = Вт, ...
    searchQuery: '',
    weekDays: [],
    skippedPairs: new Set(), // Набор "YYYY-MM-DD:pairNum"
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
        return;
      } catch (e) {
        console.warn('Не удалось распарсить hash данные:', e);
      }
    }

    // 2. Попытка чтения из localStorage
    const cached = localStorage.getItem('ktmu_schedule_cache');
    if (cached) {
      try {
        const parsed = JSON.parse(cached);
        applyParsedData(parsed);
        return;
      } catch (e) {
        console.warn('Ошибка кэша:', e);
      }
    }

    // 3. Fallback демонстрационные данные
    applyMockData();
  }

  function applyParsedData(data) {
    state.groupName = data.group_name || data.group || 'Моя группа';
    state.weekNumber = data.week_number || 4;
    state.isEvenWeek = !!data.is_even;
    state.weekDays = data.days || [];
    if (data.subgroup !== undefined) {
      state.selectedSubgroup = parseInt(data.subgroup, 10) || 0;
    }
  }

  function applyMockData() {
    state.groupName = '1-КПД-2';
    state.weekNumber = 4;
    state.isEvenWeek = false;

    // Генерируем тестовую неделю от текущего понедельника
    const now = new Date();
    const dayOfWeek = (now.getDay() + 6) % 7; // 0 = Пн, 6 = Вс
    const monday = new Date(now);
    monday.setDate(now.getDate() - dayOfWeek);

    const days = [];
    for (let i = 0; i < 6; i++) {
      const d = new Date(monday);
      d.setDate(monday.getDate() + i);
      const isoDate = d.toISOString().split('T')[0];

      let lessons = [];
      if (i < 5) {
        lessons = [
          {
            pair_number: 1,
            time: '08:30-10:00',
            subject: 'Информационные технологии',
            lesson_type: 'Лекция',
            room: '202',
            teacher: 'Иванов И.И.',
            subgroup: 0
          },
          {
            pair_number: 2,
            time: '10:10-11:40',
            subject: 'Программирование Python',
            lesson_type: 'Практика',
            room: '304 (Вознесенский)',
            teacher: 'Петров П.П.',
            subgroup: 1
          },
          {
            pair_number: 2,
            time: '10:10-11:40',
            subject: 'Компьютерные сети',
            lesson_type: 'Лаб',
            room: '105',
            teacher: 'Сидоров С.С.',
            subgroup: 2
          },
          {
            pair_number: 3,
            time: '11:50-13:20',
            subject: 'Высшая математика',
            lesson_type: 'Практика',
            room: '401',
            teacher: 'Смирнова А.В.',
            subgroup: 0
          }
        ];
      }
      days.push({
        date: isoDate,
        day_name: RU_WEEKDAYS[i],
        lessons: lessons
      });
    }
    state.weekDays = days;
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

    // Активная кнопка подгруппы
    document.querySelectorAll('.subgroup-btn').forEach(btn => {
      const sub = parseInt(btn.getAttribute('data-subgroup'), 10);
      btn.classList.toggle('active', sub === state.selectedSubgroup);
    });
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

      // Фильтрация пар под выбранную подгруппу для подсчета счетчика
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
      const lSub = l.subgroup || 0;
      if (state.selectedSubgroup > 0 && lSub > 0 && lSub !== state.selectedSubgroup) {
        return false;
      }
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
    const card = document.createElement('div');
    const pairNum = lesson.pair_number || 1;
    const skipKey = `${dateStr}:${pairNum}`;
    const isSkipped = state.skippedPairs.has(skipKey);

    card.className = `lesson-card ${isSkipped ? 'skipped' : ''}`;

    // Определение стиля типа занятия
    let typeClass = '';
    const lType = (lesson.lesson_type || '').toLowerCase();
    if (lType.includes('лекц')) typeClass = 'type-lecture';
    else if (lType.includes('практ')) typeClass = 'type-practice';
    else if (lType.includes('лаб')) typeClass = 'type-lab';

    // Аудитория с подсветкой корпуса
    const room = lesson.room || 'Не указана';
    const isVozn = room.includes('Вознесен') || lesson.is_external;
    const roomBadge = isVozn ? `🏫 ${room}` : `🚪 Ауд. ${room}`;

    card.innerHTML = `
      <div class="lesson-top">
        <div class="lesson-time-badge">
          <span class="pair-number">${pairNum}</span>
          <span class="pair-time">${lesson.time || ''}</span>
        </div>
        ${lesson.lesson_type ? `<span class="lesson-type-badge ${typeClass}">${lesson.lesson_type}</span>` : ''}
      </div>

      <div class="lesson-subject">${lesson.subject || 'Учебное занятие'}</div>

      <div class="lesson-details">
        <span class="detail-item room">${roomBadge}</span>
        ${lesson.teacher ? `<span class="detail-item">👤 ${lesson.teacher}</span>` : ''}
        ${lesson.subgroup ? `<span class="detail-item subgroup">👥 ${lesson.subgroup} подгруппа</span>` : ''}
      </div>

      <div class="lesson-bottom">
        <button class="skip-toggle-btn">
          ${isSkipped ? '💤 Пропущена (нажмите чтобы вернуть)' : '💤 Не иду на пару'}
        </button>
      </div>
    `;

    // Клик по кнопке пропуска пары
    const skipBtn = card.querySelector('.skip-toggle-btn');
    skipBtn.addEventListener('click', () => {
      triggerHaptic('impact');
      if (state.skippedPairs.has(skipKey)) {
        state.skippedPairs.delete(skipKey);
        showToast(`Пара №${pairNum} отмечена как посещаемая ✅`);
      } else {
        state.skippedPairs.add(skipKey);
        showToast(`Пара №${pairNum} отмечена как пропущенная 💤`);
      }
      renderSchedule();
      updateLiveWidget();
    });

    return card;
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
    // Выбор подгруппы
    document.querySelectorAll('.subgroup-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        triggerHaptic('selection');
        state.selectedSubgroup = parseInt(btn.getAttribute('data-subgroup'), 10);
        renderHeader();
        renderDaysNav();
        renderSchedule();
        updateLiveWidget();
      });
    });

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
      if (navigator.share) {
        navigator.share({
          title: `Расписание ${state.groupName}`,
          text: `Расписание занятий группы ${state.groupName} в КТМУ`,
          url: window.location.href
        }).catch(() => {});
      } else {
        navigator.clipboard?.writeText(window.location.href);
        showToast('Ссылка скопирована в буфер обмена 📋');
      }
    });

    // Кнопка «Обновить»
    document.getElementById('btn-refresh').addEventListener('click', () => {
      triggerHaptic('success');
      showToast('Расписание обновлено 🔄');
      renderSchedule();
      updateLiveWidget();
    });
  }

  // --- Инициализация приложения ---
  function init() {
    loadScheduleData();
    state.selectedDayIndex = determineDefaultDayIndex();
    renderHeader();
    renderDaysNav();
    renderSchedule();
    updateLiveWidget();
    setupEventListeners();

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
