/* 导航页：把右栏里的面板变成可以进入的整页，让「长期记忆 / 需要你决定 / 活动记录 /
   定时任务」点得进去，也能用地址栏 #memory 这样的锚点直接打开。总览保持原样。 */
(function () {
  'use strict';
  var PAGES = ['home', 'memory', 'requests', 'activity', 'schedule'];
  var MOVABLE = ['memory', 'requests', 'activity'];
  var slots = {};
  var host = null;
  var current = null;

  function mainNode() { return document.getElementById('home'); }

  function ensureHost() {
    if (host) return host;
    host = document.getElementById('pageHost');
    if (!host) {
      host = document.createElement('div');
      host.id = 'pageHost';
      host.hidden = true;
      mainNode().appendChild(host);
    }
    return host;
  }

  function slotFor(id) {
    if (slots[id]) return slots[id];
    var section = document.getElementById(id);
    if (!section || !section.parentNode) return null;
    var slot = document.createElement('span');
    slot.className = 'page-slot';
    slot.setAttribute('data-slot', id);
    section.parentNode.insertBefore(slot, section);
    slots[id] = slot;
    return slot;
  }

  function moveIn(id) {
    var section = document.getElementById(id);
    if (!section) return null;
    slotFor(id);
    ensureHost().appendChild(section);
    section.classList.add('page-view');
    return section;
  }

  function moveOut(id) {
    var section = document.getElementById(id);
    var slot = slots[id];
    if (!section || !slot || !slot.parentNode) return;
    if (section.parentNode !== slot.parentNode) slot.parentNode.insertBefore(section, slot);
    section.classList.remove('page-view');
  }

  function markActive(page) {
    var links = document.querySelectorAll('nav a[href^="#"]');
    for (var i = 0; i < links.length; i++) {
      var target = links[i].getAttribute('href').slice(1);
      if (target === 'settings') continue;
      links[i].classList.toggle('active', target === page);
    }
  }

  function show(page) {
    if (PAGES.indexOf(page) < 0) page = 'home';
    var view = ensureHost();
    for (var i = 0; i < MOVABLE.length; i++) {
      if (page !== MOVABLE[i]) moveOut(MOVABLE[i]);
    }
    if (page === 'home') {
      view.hidden = true;
      document.body.setAttribute('data-page', 'home');
    } else {
      if (page === 'schedule') {
        if (!document.getElementById('schedule') && window.DshSchedule) window.DshSchedule.build();
        var card = document.getElementById('schedule');
        if (card) { view.appendChild(card); card.classList.add('page-view'); }
      } else {
        moveIn(page);
      }
      view.hidden = false;
      document.body.setAttribute('data-page', page);
      window.scrollTo(0, 0);
    }
    current = page;
    markActive(page);
    if (page === 'schedule' && window.DshSchedule) window.DshSchedule.activate();
    if (page === 'memory' && typeof window.loadMemories === 'function') window.loadMemories();
  }

  function route() {
    var name = (location.hash || '#home').slice(1);
    if (name === 'settings' || name === '/settings') return;
    show(PAGES.indexOf(name) >= 0 ? name : 'home');
  }

  document.addEventListener('click', function (event) {
    var link = event.target.closest ? event.target.closest('nav a[href^="#"]') : null;
    if (!link) return;
    var name = link.getAttribute('href').slice(1);
    if (name === 'settings') return;
    if (PAGES.indexOf(name) < 0) return;
    event.preventDefault();
    if (current === name) return;
    if (history.replaceState) history.replaceState(null, '', '#' + name);
    show(name);
  });

  window.addEventListener('hashchange', function () {
    if (location.hash === '#settings' || location.hash === '#/settings') return;
    route();
  });

  window.DshPages = {show: show, route: route};
  // defer 脚本按顺序执行；等后面的 schedule.js 也注册好再落第一次路由。
  setTimeout(route, 0);
})();
