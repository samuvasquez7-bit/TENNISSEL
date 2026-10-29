'use strict';

(() => {
    const script = document.currentScript;
    const copy = document.getElementById('rewardPromoCopy');
    const fill = document.getElementById('rewardProgressFill');
    const progress = document.getElementById('rewardProgressTrack');
    const victories = document.getElementById('rewardVictoryCount');
    const remaining = document.getElementById('rewardRemainingCount');
    const available = document.getElementById('rewardAvailableCount');
    const claim = document.getElementById('rewardClaimButton');
    const result = document.getElementById('rewardResult');
    const prize = document.getElementById('rewardPrize');
    const code = document.getElementById('rewardCode');
    const status = document.getElementById('rewardStatus');
    const challengeList = document.getElementById('rewardChallenges');
    const cyclesList = document.getElementById('rewardCycleHistory');
    const couponsList = document.getElementById('rewardCouponHistory');
    const challengeHistoryList = document.getElementById('rewardChallengeHistory');

    const replaceList = (target, entries, emptyLabel, renderEntry) => {
        target.replaceChildren();
        if (!entries.length) {
            const empty = document.createElement('li');
            empty.textContent = emptyLabel;
            target.append(empty);
            return;
        }
        entries.forEach((entry) => {
            const item = document.createElement('li');
            renderEntry(item, entry);
            target.append(item);
        });
    };

    const formatDate = (value) => {
        if (!value) return 'Sin fecha';
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? 'Sin fecha' : date.toLocaleString('es-CO');
    };

    const renderHistory = (data) => {
        replaceList(cyclesList, data.ciclos || [], 'Aún no has completado un ciclo.', (item, cycle) => {
            const prize = cycle.premio_valor ? ` · ${cycle.premio_valor}` : '';
            item.textContent = `Ciclo ${cycle.ciclo}: ${cycle.estado}${prize} · ${formatDate(cycle.desbloqueado_at)}`;
        });
        replaceList(couponsList, data.cupones || [], 'No tienes cupones vigentes.', (item, coupon) => {
            const label = document.createElement('span');
            label.className = 'reward-coupon-code';
            label.textContent = coupon.codigo;
            item.append(label, document.createTextNode(` · ${coupon.tipo === 'envio' ? 'Envío gratis' : coupon.valor} · vence ${formatDate(coupon.vence_at)}`));
        });
        replaceList(challengeHistoryList, data.historial_desafios || [], 'Aún no tienes retos en el historial.', (item, challenge) => {
            item.textContent = `${challenge.titulo || challenge.id}: ${challenge.estado} (${challenge.progreso}/${challenge.objetivo}) · ${formatDate(challenge.vence_at)}`;
        });
    };

    const renderChallenges = (challenges) => {
        challengeList.replaceChildren();
        (challenges || []).forEach((challenge) => {
            const card = document.createElement('article');
            card.className = 'reward-challenge';
            const title = document.createElement('strong');
            title.textContent = challenge.titulo;
            const description = document.createElement('p');
            description.textContent = challenge.descripcion;
            const progressBar = document.createElement('progress');
            progressBar.max = challenge.objetivo;
            progressBar.value = challenge.progreso;
            progressBar.setAttribute('aria-label', `Progreso: ${challenge.progreso} de ${challenge.objetivo}`);
            const details = document.createElement('small');
            details.textContent = `${challenge.dificultad} · ${challenge.progreso}/${challenge.objetivo} · Recompensa: ${challenge.tipo_recompensa === 'experiencia' ? `${challenge.valor_recompensa} XP` : challenge.valor_recompensa} · ${challenge.estado}`;
            const deadline = document.createElement('small');
            deadline.dataset.deadline = challenge.vence_at || '';
            deadline.textContent = `Vence: ${formatDate(challenge.vence_at)}`;
            card.append(title, description, progressBar, details, deadline);
            if (challenge.estado === 'completado' && !challenge.reclamado) {
                const button = document.createElement('button');
                button.type = 'button';
                button.textContent = 'Reclamar premio';
                button.dataset.challengeId = challenge.id;
                button.addEventListener('click', () => claimChallenge(button, challenge));
                card.append(button);
            }
            challengeList.append(card);
        });
        updateCountdowns();
    };

    const updateCountdowns = () => {
        challengeList.querySelectorAll('[data-deadline]').forEach((label) => {
            const deadline = new Date(label.dataset.deadline).getTime();
            const remainingMs = deadline - Date.now();
            if (!Number.isFinite(deadline)) return;
            if (remainingMs <= 0) {
                label.textContent = 'Vencido · actualiza los retos para renovar';
                label.closest('.reward-challenge')?.querySelector('button')?.setAttribute('disabled', '');
                return;
            }
            let seconds = Math.floor(remainingMs / 1000);
            const days = Math.floor(seconds / 86400);
            seconds %= 86400;
            const hours = Math.floor(seconds / 3600);
            seconds %= 3600;
            const minutes = Math.floor(seconds / 60);
            seconds %= 60;
            label.textContent = `Restante: ${days}d ${hours}h ${minutes}m ${seconds}s · vence ${formatDate(label.dataset.deadline)}`;
        });
    };

    const claimChallenge = async (button, challenge) => {
        button.disabled = true;
        status.textContent = 'Asignando tu recompensa…';
        try {
            const response = await fetch(script.dataset.challengeClaimUrl, {
                method: 'POST',
                headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
                body: JSON.stringify({ desafio_id: challenge.id })
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'No se pudo reclamar el reto.');
            status.textContent = data.codigo
                ? `Premio asignado. Cupón: ${data.codigo}`
                : `Premio asignado: ${data.recompensa.valor} ${data.recompensa.tipo === 'experiencia' ? 'XP' : ''}`;
            await loadProgress();
        } catch (error) {
            status.textContent = error.message;
            button.disabled = false;
        }
    };

    const showProgress = (data) => {
        const wins = Number(data.victorias);
        const coupons = Number(data.giros);
        const nextReward = Number(data.restantes);
        if (![wins, coupons, nextReward].every(Number.isFinite)) {
            throw new Error('La respuesta de recompensas no es válida.');
        }

        const percentage = wins % 100;
        progress.hidden = false;
        progress.setAttribute('aria-valuenow', String(percentage));
        fill.style.width = `${percentage}%`;
        victories.textContent = `Victorias registradas: ${wins}`;
        remaining.textContent = `Faltan ${nextReward} para el próximo cupón`;
        available.textContent = `Descuentos disponibles: ${coupons}`;
        copy.textContent = `Cada 100 victorias confirmadas por el servidor desbloqueas una jugada. Llevas ${wins} victorias.`;
        claim.hidden = coupons < 1;
        claim.disabled = coupons < 1;
        renderChallenges(data.desafios || []);
        renderHistory(data);
    };

    const loadProgress = async () => {
        try {
            const response = await fetch(script.dataset.progressUrl, {
                headers: { Accept: 'application/json' }
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'No se pudo consultar el progreso.');
            if (!data.autenticado) {
                copy.textContent = 'Inicia sesión y gana partidos para desbloquear cupones de descuento en el catálogo.';
                victories.textContent = 'Inicia sesión para consultar tus victorias';
                remaining.textContent = 'Progreso personal';
                available.textContent = 'Descuentos disponibles: inicia sesión';
                status.textContent = 'Inicia sesión para guardar y consultar tu progreso.';
                renderChallenges([]);
                return;
            }
            showProgress(data);
        } catch (error) {
            status.textContent = error.message;
        }
    };

    claim.addEventListener('click', async () => {
        claim.disabled = true;
        status.textContent = 'Preparando tu cupón…';
        try {
            const response = await fetch(script.dataset.claimUrl, {
                method: 'POST',
                headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
                body: '{}'
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'No se pudo reclamar el descuento.');
            if (!data.premio || !data.codigo) throw new Error('La respuesta del cupón no es válida.');
            prize.textContent = data.premio.tipo === 'envio' ? '¡Envío gratis!' : `¡${data.premio.valor} de descuento!`;
            code.textContent = data.codigo;
            result.hidden = false;
            status.textContent = 'Copia el código y aplícalo en el carrito.';
            await loadProgress();
        } catch (error) {
            status.textContent = error.message;
        } finally {
            claim.disabled = false;
        }
    });

    window.addEventListener('tennisArenaRewardUpdated', loadProgress);
    window.setInterval(updateCountdowns, 1000);

    document.getElementById('rewardCopyButton').addEventListener('click', async () => {
        try {
            if (!navigator.clipboard) throw new Error('El portapapeles no está disponible.');
            await navigator.clipboard.writeText(code.textContent);
            status.textContent = 'Código copiado.';
        } catch {
            status.textContent = 'Selecciona y copia el código manualmente.';
        }
    });

    const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');
    const revealItems = document.querySelectorAll('.games-reveal');
    if (motionPreference.matches || !('IntersectionObserver' in window)) {
        revealItems.forEach((item) => item.classList.add('is-visible'));
    } else {
        document.documentElement.classList.add('games-motion-ready');
        const observer = new IntersectionObserver((entries, currentObserver) => {
            entries.forEach((entry) => {
                if (!entry.isIntersecting) return;
                entry.target.classList.add('is-visible');
                currentObserver.unobserve(entry.target);
            });
        }, { threshold: 0.12, rootMargin: '0px 0px -24px 0px' });
        revealItems.forEach((item) => observer.observe(item));
    }

    const pointerPreference = window.matchMedia('(hover: hover) and (pointer: fine)');
    if (pointerPreference.matches && !motionPreference.matches) {
        document.querySelectorAll('.tilt-card, .games-info-item').forEach((card) => {
            card.addEventListener('pointermove', (event) => {
                const bounds = card.getBoundingClientRect();
                const x = (event.clientX - bounds.left) / bounds.width;
                const y = (event.clientY - bounds.top) / bounds.height;
                const maxTilt = card.classList.contains('tilt-card') ? 5 : 2.5;
                card.style.setProperty('--tilt-x', `${(0.5 - y) * maxTilt}deg`);
                card.style.setProperty('--tilt-y', `${(x - 0.5) * maxTilt}deg`);
                card.style.setProperty('--pointer-x', `${x * 100}%`);
                card.style.setProperty('--pointer-y', `${y * 100}%`);
                card.classList.add('is-pointer-active');
            });
            card.addEventListener('pointerleave', () => {
                card.style.setProperty('--tilt-x', '0deg');
                card.style.setProperty('--tilt-y', '0deg');
                card.classList.remove('is-pointer-active');
            });
        });
    }

    loadProgress();
})();
