(function () {
  'use strict';

  /* ── Table des matières (TOC) ───────────────────────────────────────── */
  var tocList    = document.getElementById('toc-list');
  var articleBody = document.querySelector('.article-body');

  if (tocList && articleBody) {
    var headings = articleBody.querySelectorAll('h2');

    if (headings.length === 0) {
      var tocEl = document.querySelector('.article-toc');
      if (tocEl) tocEl.style.display = 'none';
    } else {

      function toSlug(text) {
        return text
          .toLowerCase()
          .normalize('NFD')
          .replace(/[̀-ͯ]/g, '')
          .replace(/[^a-z0-9]+/g, '-')
          .replace(/^-+|-+$/g, '');
      }

      var tocLinks = [];

      headings.forEach(function (h2) {
        if (!h2.id) {
          h2.id = toSlug(h2.textContent.trim());
        }

        var li = document.createElement('li');
        var a  = document.createElement('a');
        a.href      = '#' + h2.id;
        a.className = 'article-toc-link';
        a.textContent = h2.textContent.trim();

        a.addEventListener('click', function (e) {
          e.preventDefault();
          var target = document.getElementById(h2.id);
          if (!target) return;
          var y = target.getBoundingClientRect().top + window.pageYOffset - 90;
          window.scrollTo({ top: y, behavior: 'smooth' });
        });

        li.appendChild(a);
        tocList.appendChild(li);
        tocLinks.push({ el: h2, link: a });
      });

      /* Scroll spy avec IntersectionObserver */
      if ('IntersectionObserver' in window && tocLinks.length > 0) {
        var activeLink = null;

        var observer = new IntersectionObserver(function (entries) {
          entries.forEach(function (entry) {
            if (entry.isIntersecting) {
              var found = tocLinks.find(function (t) { return t.el === entry.target; });
              if (found) {
                if (activeLink) activeLink.classList.remove('active');
                activeLink = found.link;
                activeLink.classList.add('active');
              }
            }
          });
        }, { rootMargin: '-80px 0px -60% 0px', threshold: 0 });

        /* Activer le premier lien au chargement */
        if (tocLinks.length > 0) {
          tocLinks[0].link.classList.add('active');
          activeLink = tocLinks[0].link;
        }

        tocLinks.forEach(function (t) { observer.observe(t.el); });
      }
    }
  }

  /* ── Boutons de partage ──────────────────────────────────────────────── */
  var pageUrl   = window.location.href;
  var pageTitle = document.title;

  document.querySelectorAll('[data-share="linkedin"]').forEach(function (btn) {
    btn.href   = 'https://www.linkedin.com/sharing/share-offsite/?url=' + encodeURIComponent(pageUrl);
    btn.target = '_blank';
    btn.rel    = 'noopener noreferrer';
  });

  document.querySelectorAll('[data-share="twitter"]').forEach(function (btn) {
    btn.href   = 'https://twitter.com/intent/tweet?url=' + encodeURIComponent(pageUrl) + '&text=' + encodeURIComponent(pageTitle);
    btn.target = '_blank';
    btn.rel    = 'noopener noreferrer';
  });

  var copyBtn = document.getElementById('share-copy-btn');
  if (copyBtn) {
    if (!navigator.clipboard) {
      copyBtn.style.display = 'none';
    } else {
      copyBtn.addEventListener('click', function () {
        navigator.clipboard.writeText(pageUrl).then(function () {
          var originalHTML = copyBtn.innerHTML;
          copyBtn.textContent = 'Lien copie !';
          copyBtn.classList.add('copied');
          setTimeout(function () {
            copyBtn.innerHTML = originalHTML;
            copyBtn.classList.remove('copied');
          }, 2000);
        });
      });
    }
  }

})();
