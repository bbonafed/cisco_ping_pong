// Loading Overlay Management
function showLoading(message = 'Loading...') {
    const overlay = document.getElementById('loading-overlay');
    const text = document.getElementById('loading-text');
    if (overlay) {
        if (text) text.textContent = message;
        overlay.classList.add('active');
    }
}

function hideLoading() {
    const overlay = document.getElementById('loading-overlay');
    if (overlay) {
        overlay.classList.remove('active');
    }
}

const SVG_NS = 'http://www.w3.org/2000/svg';
let bracketDrawScheduled = false;

function renderBracketConnector(svg, wrapperRect, bracket, matchEl) {
    const nextRound = matchEl.dataset.nextRound;
    const nextIndex = matchEl.dataset.nextIndex;
    if (!nextRound || !nextIndex) {
        return;
    }

    const target = bracket.querySelector(
        `.bracket-match[data-round="${nextRound}"][data-index="${nextIndex}"]`
    );
    if (!target) {
        return;
    }

    const sourceRect = matchEl.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();

    const startX = sourceRect.right - wrapperRect.left;
    const startY = sourceRect.top - wrapperRect.top + sourceRect.height / 2;
    const endX = targetRect.left - wrapperRect.left;
    const endY = targetRect.top - wrapperRect.top + targetRect.height / 2;

    const offsetStartX = startX + 8;
    const offsetEndX = Math.max(offsetStartX + 24, endX - 8);
    const controlOffset = Math.max(48, (offsetEndX - offsetStartX) * 0.5);

    const path = document.createElementNS(SVG_NS, 'path');
    const d = `M ${offsetStartX} ${startY} C ${offsetStartX + controlOffset} ${startY}, ${offsetEndX - controlOffset} ${endY}, ${offsetEndX} ${endY}`;
    path.setAttribute('d', d);
    path.classList.add('bracket-connector');
    if (matchEl.dataset.state === 'complete') {
        path.classList.add('is-complete');
    }
    svg.appendChild(path);
}

function drawBracketConnectors() {
    const wrappers = document.querySelectorAll('.bracket-wrapper');

    for (const wrapper of wrappers) {
        const svg = wrapper.querySelector('.bracket-connector-layer');
        const bracket = wrapper.querySelector('.bracket');
        if (!svg || !bracket) {
            continue;
        }

        const matches = bracket.querySelectorAll('.bracket-match[data-round]');
        if (matches.length === 0) {
            svg.innerHTML = '';
            svg.setAttribute('width', 0);
            svg.setAttribute('height', 0);
            continue;
        }

        const wrapperRect = wrapper.getBoundingClientRect();
        const width = Math.max(wrapper.scrollWidth, wrapperRect.width);
        const height = Math.max(wrapper.scrollHeight, wrapperRect.height);

        svg.setAttribute('width', width);
        svg.setAttribute('height', height);
        svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
        svg.innerHTML = '';

        for (const matchEl of matches) {
            renderBracketConnector(svg, wrapperRect, bracket, matchEl);
        }
    }
}

function scheduleBracketDraw() {
    if (bracketDrawScheduled) {
        return;
    }
    bracketDrawScheduled = true;
    globalThis.requestAnimationFrame(() => {
        bracketDrawScheduled = false;
        drawBracketConnectors();
    });
}

// Add loading spinner to forms on submit
document.addEventListener('DOMContentLoaded', function() {
    document.documentElement.classList.add('js-enabled');
    // Add loading to all forms except search forms
    const forms = document.querySelectorAll('form:not(.no-loading)');
    for (const form of forms) {
        form.addEventListener('submit', function(e) {
            // Don't show loading for GET forms (search)
            if (form.method.toLowerCase() !== 'get') {
                showLoading('Processing...');
            }
        });
    }

    // Player Search/Filter Functionality
    const searchInput = document.getElementById('player-search');
    if (searchInput) {
        searchInput.addEventListener('input', function(e) {
            const searchTerm = e.target.value.toLowerCase();
            const rows = document.querySelectorAll('.searchable-row');

            for (const row of rows) {
                const text = row.textContent.toLowerCase();
                if (text.includes(searchTerm)) {
                    row.style.display = '';
                } else {
                    row.style.display = 'none';
                }
            }
        });
    }

    // Confirmation dialogs for destructive actions
    const dangerButtons = document.querySelectorAll('.btn-danger, [data-confirm]');
    for (const button of dangerButtons) {
        button.addEventListener('click', function(e) {
            const message = button.dataset.confirm || 'Are you sure you want to proceed?';
            if (!confirm(message)) {
                e.preventDefault();
                return false;
            }
        });
    }

    // Auto-hide flash messages after 5 seconds
    const flashMessages = document.querySelectorAll('.flash');
    for (const flash of flashMessages) {
        setTimeout(() => {
            flash.style.transition = 'opacity 0.5s ease';
            flash.style.opacity = '0';
            setTimeout(() => flash.remove(), 500);
        }, 5000);
    }

    scheduleBracketDraw();
    setTimeout(scheduleBracketDraw, 150);
    setTimeout(scheduleBracketDraw, 400);
    globalThis.addEventListener('resize', scheduleBracketDraw);

    if (globalThis.ResizeObserver) {
        const observer = new ResizeObserver(() => scheduleBracketDraw());
        const wrapperNodes = document.querySelectorAll('.bracket-wrapper');
        for (const wrapper of wrapperNodes) {
            observer.observe(wrapper);
        }
    }

    const navToggle = document.getElementById('nav-toggle');
    const navMenu = document.getElementById('nav-menu');
    if (navToggle && navMenu) {
        const closeMenu = () => {
            navMenu.classList.remove('is-open');
            navToggle.setAttribute('aria-expanded', 'false');
        };

        const openMenu = () => {
            navMenu.classList.add('is-open');
            navToggle.setAttribute('aria-expanded', 'true');
        };

        navToggle.addEventListener('click', (event) => {
            event.stopPropagation();
            if (navMenu.classList.contains('is-open')) {
                closeMenu();
            } else {
                openMenu();
            }
        });

        document.addEventListener('click', (event) => {
            if (!navMenu.contains(event.target) && event.target !== navToggle) {
                closeMenu();
            }
        });

        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') {
                closeMenu();
            }
        });

        const menuLinks = navMenu.querySelectorAll('a');
        for (const link of menuLinks) {
            link.addEventListener('click', () => closeMenu());
        }
    }

    const bookingForm = document.getElementById('calendar-book-form');
    if (bookingForm) {
        const weekdayField = document.getElementById('booking-weekday');
        const slotField = document.getElementById('booking-start');
        const modal = document.getElementById('booking-modal');
        const modalDetails = document.getElementById('booking-modal-details');
        const confirmButton = document.getElementById('booking-confirm');
        const cancelButton = document.getElementById('booking-cancel');
        const calendarCells = document.querySelectorAll('.calendar-cell.is-available');

        let pendingSelection = null;
        let selectedCell = null;

        const resetSelection = () => {
            pendingSelection = null;
            if (selectedCell) {
                selectedCell.classList.remove('is-selected');
                selectedCell = null;
            }
        };

        const closeModal = () => {
            if (modal) {
                if (modal.open && typeof modal.close === 'function') {
                    modal.close();
                } else {
                    modal.removeAttribute('open');
                }
            }
            resetSelection();
        };

        const openModal = (message) => {
            if (!modal) {
                return;
            }
            if (modalDetails && message) {
                modalDetails.textContent = message;
            }
            if (!modal.open) {
                if (typeof modal.showModal === 'function') {
                    modal.showModal();
                } else {
                    modal.setAttribute('open', 'true');
                }
            }
        };

        const buildMessage = (dayLabel, timeLabel, endLabel) => {
            if (dayLabel && timeLabel && endLabel) {
                return `Reserve ${dayLabel}, ${timeLabel} – ${endLabel}?`;
            }
            if (dayLabel && timeLabel) {
                return `Reserve ${dayLabel} at ${timeLabel}?`;
            }
            return 'Reserve this slot?';
        };

        for (const cell of calendarCells) {
            cell.addEventListener('click', () => {
                const weekday = cell.dataset.weekday;
                const slot = cell.dataset.slot;

                if (weekday === undefined || slot === undefined) {
                    return;
                }

                const dayLabel = cell.dataset.dayLabel || '';
                const timeLabel = cell.dataset.timeLabel || '';
                const endLabel = cell.dataset.endLabel || '';

                pendingSelection = { weekday, slot };

                if (selectedCell && selectedCell !== cell) {
                    selectedCell.classList.remove('is-selected');
                }
                selectedCell = cell;
                selectedCell.classList.add('is-selected');

                openModal(buildMessage(dayLabel, timeLabel, endLabel));
            });
        }

        if (confirmButton) {
            confirmButton.addEventListener('click', () => {
                if (!pendingSelection) {
                    closeModal();
                    return;
                }

                const { weekday, slot } = pendingSelection;
                weekdayField.value = weekday;
                slotField.value = slot;
                if (typeof showLoading === 'function') {
                    showLoading('Saving slot...');
                }
                closeModal();
                bookingForm.submit();
            });
        }

        const cancelHandler = () => closeModal();
        cancelButton?.addEventListener('click', cancelHandler);
        modal?.addEventListener('cancel', (event) => {
            event.preventDefault();
            closeModal();
        });
        modal?.addEventListener('close', resetSelection);
    }
});

// Player profile quick view
function viewPlayerProfile(playerId) {
    globalThis.location.href = `/player/${playerId}`;
}
