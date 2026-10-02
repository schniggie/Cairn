(() => {
  const selector = 'select:not([multiple]):not([data-cairn-native-select])';
  const layoutPrefixes = ['w-', 'min-w-', 'max-w-', 'mt-', 'mb-', 'ml-', 'mr-', 'mx-', 'my-'];
  let activeControl = null;
  let controlSequence = 0;
  let initialized = false;

  function closeActive({ restoreFocus = false } = {}) {
    if (!activeControl) return;
    const { button, menu } = activeControl;
    menu.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    activeControl = null;
    if (restoreFocus && button.isConnected) button.focus();
  }

  function positionMenu(button, menu) {
    const rect = button.getBoundingClientRect();
    const viewportGap = 8;
    const width = Math.max(rect.width, 180);
    const availableBelow = window.innerHeight - rect.bottom - viewportGap;
    const openAbove = availableBelow < 180 && rect.top > availableBelow;
    menu.style.width = `${Math.min(width, window.innerWidth - viewportGap * 2)}px`;
    menu.style.left = `${Math.min(Math.max(viewportGap, rect.left), window.innerWidth - width - viewportGap)}px`;
    if (openAbove) {
      menu.style.top = 'auto';
      menu.style.bottom = `${window.innerHeight - rect.top + 6}px`;
      menu.style.maxHeight = `${Math.max(120, rect.top - viewportGap * 2)}px`;
    } else {
      menu.style.top = `${rect.bottom + 6}px`;
      menu.style.bottom = 'auto';
      menu.style.maxHeight = `${Math.max(120, availableBelow)}px`;
    }
  }

  function enhanceSelect(select) {
    if (!(select instanceof HTMLSelectElement) || select.dataset.cairnEnhanced === 'true') return;
    select.dataset.cairnEnhanced = 'true';

    const originalClasses = [...select.classList];
    const wrapper = document.createElement('span');
    wrapper.className = 'cairn-select';
    originalClasses
      .filter(name => name === 'flex-1' || layoutPrefixes.some(prefix => name.startsWith(prefix)))
      .forEach(name => wrapper.classList.add(name));
    if (originalClasses.some(name => ['text-[10px]', 'text-[11px]', 'text-xs'].includes(name))) {
      wrapper.classList.add('cairn-select--compact');
    }
    if (originalClasses.includes('border-0') || originalClasses.includes('bg-transparent')) {
      wrapper.classList.add('cairn-select--ghost');
    }

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'cairn-select__button';
    button.setAttribute('role', 'combobox');
    button.setAttribute('aria-haspopup', 'listbox');
    button.setAttribute('aria-expanded', 'false');

    const label = document.createElement('span');
    label.className = 'cairn-select__label';
    const chevron = document.createElement('span');
    chevron.className = 'cairn-select__chevron';
    chevron.setAttribute('aria-hidden', 'true');
    chevron.innerHTML = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m6 8 4 4 4-4"/></svg>';
    button.append(label, chevron);

    const menu = document.createElement('div');
    menu.className = 'cairn-select__menu';
    menu.id = `cairn-select-menu-${++controlSequence}`;
    menu.setAttribute('role', 'listbox');
    menu.hidden = true;
    button.setAttribute('aria-controls', menu.id);

    select.parentNode.insertBefore(wrapper, select);
    wrapper.append(select, button);
    document.body.appendChild(menu);
    select.classList.add('cairn-select__native');
    select.setAttribute('aria-hidden', 'true');
    select.tabIndex = -1;

    function sync() {
      if (!select.isConnected) {
        menu.remove();
        return;
      }
      const selectedOption = select.options[select.selectedIndex];
      label.textContent = selectedOption?.textContent?.trim() || '—';
      button.disabled = select.disabled;
      wrapper.hidden = select.hidden || select.style.display === 'none';
      menu.innerHTML = '';
      [...select.options].forEach((option, index) => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'cairn-select__option';
        item.setAttribute('role', 'option');
        item.setAttribute('aria-selected', option.selected ? 'true' : 'false');
        item.disabled = option.disabled;
        item.dataset.value = option.value;
        const text = document.createElement('span');
        text.textContent = option.textContent?.trim() || option.value;
        const check = document.createElement('span');
        check.className = 'cairn-select__check';
        check.setAttribute('aria-hidden', 'true');
        check.textContent = '✓';
        item.append(text, check);
        item.addEventListener('click', event => {
          event.stopPropagation();
          if (option.disabled) return;
          select.selectedIndex = index;
          select.dispatchEvent(new Event('input', { bubbles: true }));
          select.dispatchEvent(new Event('change', { bubbles: true }));
          sync();
          closeActive({ restoreFocus: true });
        });
        menu.appendChild(item);
      });
    }

    function open() {
      sync();
      if (button.disabled || wrapper.hidden) return;
      if (activeControl?.button === button) {
        closeActive({ restoreFocus: true });
        return;
      }
      closeActive();
      positionMenu(button, menu);
      menu.hidden = false;
      button.setAttribute('aria-expanded', 'true');
      activeControl = { select, wrapper, button, menu };
      requestAnimationFrame(() => {
        menu.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
      });
    }

    button.addEventListener('click', event => {
      event.stopPropagation();
      open();
    });
    button.addEventListener('keydown', event => {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
        event.preventDefault();
        open();
        const selected = menu.querySelector('[aria-selected="true"]');
        (selected || menu.querySelector('.cairn-select__option:not(:disabled)'))?.focus();
      }
      if (event.key === 'Escape') closeActive({ restoreFocus: true });
    });
    menu.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeActive({ restoreFocus: true });
        return;
      }
      if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
      event.preventDefault();
      const options = [...menu.querySelectorAll('.cairn-select__option:not(:disabled)')];
      const current = options.indexOf(document.activeElement);
      const direction = event.key === 'ArrowDown' ? 1 : -1;
      options[(current + direction + options.length) % options.length]?.focus();
    });

    select.addEventListener('change', sync);
    select.addEventListener('input', sync);
    new MutationObserver(() => queueMicrotask(sync)).observe(select, {
      attributes: true,
      childList: true,
      characterData: true,
      subtree: true,
    });
    sync();
  }

  function scan(root = document) {
    if (root.matches?.(selector)) enhanceSelect(root);
    root.querySelectorAll?.(selector).forEach(enhanceSelect);
  }

  function initialize() {
    if (initialized) return;
    initialized = true;
    scan();
    new MutationObserver(records => {
      records.forEach(record => record.addedNodes.forEach(node => {
        if (node instanceof Element) scan(node);
      }));
    }).observe(document.body, { childList: true, subtree: true });
  }

  document.addEventListener('click', () => closeActive());
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeActive({ restoreFocus: true });
  });
  window.addEventListener('resize', () => closeActive());
  window.addEventListener('scroll', () => closeActive(), true);
  document.addEventListener('alpine:initialized', initialize, { once: true });
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(initialize, 0), { once: true });
  } else {
    setTimeout(initialize, 0);
  }
})();
