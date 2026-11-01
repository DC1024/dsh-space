/* 她的手机：控制台里的最近画面。只读，不触发动作。 */
(function () {
  'use strict';
  var image = document.getElementById('phoneShot');
  var button = document.getElementById('phoneRefresh');
  var meta = document.getElementById('phoneMeta');
  if (!image) return;

  function stamp() {
    return new Date().toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  }

  function load() {
    if (meta) meta.textContent = '正在取画面…';
    image.src = '/api/phone-shot?t=' + Date.now();
  }

  image.addEventListener('load', function () {
    if (meta) meta.textContent = '最近画面 · ' + stamp() + '（' + image.naturalWidth + '×' + image.naturalHeight + '）';
  });
  image.addEventListener('error', function () {
    if (meta) meta.textContent = '还没有手机画面：让她先看一眼手机即可生成';
  });
  if (button) button.addEventListener('click', load);
  load();
})();
