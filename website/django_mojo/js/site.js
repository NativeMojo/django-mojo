(function () {
  var reveals = document.querySelectorAll('.reveal');
  var show = function (element) { element.classList.add('in'); };
  if ('IntersectionObserver' in window && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { show(entry.target); observer.unobserve(entry.target); }
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 });
    reveals.forEach(function (element) { observer.observe(element); });
  } else {
    reveals.forEach(show);
  }
  setTimeout(function () { reveals.forEach(show); }, 2000);

  document.querySelectorAll('[data-copy]').forEach(function (button) {
    button.addEventListener('click', function () {
      var value = button.getAttribute('data-copy');
      if (!navigator.clipboard) { return; }
      navigator.clipboard.writeText(value).then(function () {
        var label = button.querySelector('b');
        label.textContent = 'Copied';
        setTimeout(function () { label.textContent = 'Copy'; }, 1400);
      });
    });
  });
})();
