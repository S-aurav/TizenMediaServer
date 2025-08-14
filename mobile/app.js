class StreamFlixApp {
    constructor() {
        this.apiBase = window.location.origin; // Use current domain
        this.currentSeries = null;
        this.currentSeason = null;
        this.currentEpisode = null;
        this.player = null;
        this.seriesList = [];
        this.currentSlideIndex = 0;
        this.slideInterval = null;
        
        this.init();
    }
    
    async init() {
        this.setupEventListeners();
        this.setupScrollHeader();
        await this.loadSeries();
        this.setupVideoPlayer();
        this.startSlideshow();
    }
    
    setupEventListeners() {
        // Series modal
        document.getElementById('closeSeriesModal').addEventListener('click', () => {
            this.closeSeriesModal();
        });
        
        // Video modal
        document.getElementById('closeVideoModal').addEventListener('click', () => {
            this.closeVideoModal();
        });
        
        // Hero buttons
        document.getElementById('heroPlayBtn').addEventListener('click', () => {
            this.playCurrentSeries();
        });
        
        document.getElementById('heroInfoBtn').addEventListener('click', () => {
            this.showCurrentSeriesInfo();
        });
        
        // Season selector
        document.getElementById('seasonSelect').addEventListener('change', (e) => {
            this.switchSeason(e.target.value);
        });
        
        // Download buttons
        document.getElementById('downloadAllSeries').addEventListener('click', () => {
            this.downloadAllSeries();
        });
        
        document.getElementById('downloadSeasonBtn').addEventListener('click', () => {
            this.downloadCurrentSeason();
        });
        
        // Close modals on outside click
        document.getElementById('seriesModal').addEventListener('click', (e) => {
            if (e.target.id === 'seriesModal') {
                this.closeSeriesModal();
            }
        });
        
        document.getElementById('videoModal').addEventListener('click', (e) => {
            if (e.target.id === 'videoModal') {
                this.closeVideoModal();
            }
        });
        
        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                this.closeAllModals();
            }
        });
    }
    
    setupScrollHeader() {
        window.addEventListener('scroll', () => {
            const header = document.querySelector('.header');
            if (window.scrollY > 100) {
                header.classList.add('scrolled');
            } else {
                header.classList.remove('scrolled');
            }
        });
    }
    
    async loadSeries() {
        try {
            this.showLoading(true);
            
            const response = await fetch(`${this.apiBase}/catalog/series`);
            const series = await response.json();
            
            this.seriesList = series;
            this.renderSeriesGrid(series);
            this.setupHeroSlideshow(series);
            
        } catch (error) {
            this.showToast('Failed to load series', 'error');
            console.error('Error loading series:', error);
        } finally {
            this.showLoading(false);
        }
    }
    
    renderSeriesGrid(series) {
        const grid = document.getElementById('seriesGrid');
        grid.innerHTML = '';
        
        series.forEach(serie => {
            const card = document.createElement('div');
            card.className = 'series-card fade-in';
            card.innerHTML = `
                <img src="${serie.poster}" alt="${serie.name}" onerror="this.src='/static/default.jpg'">
                <div class="series-card-info">
                    <div class="series-card-title">${serie.name}</div>
                    <div class="series-card-meta">Series</div>
                </div>
            `;
            
            card.addEventListener('click', () => {
                this.openSeriesModal(serie.name);
            });
            
            grid.appendChild(card);
        });
    }
    
    setupHeroSlideshow(series) {
        if (series.length === 0) return;
        
        // Setup indicators
        const indicatorsContainer = document.getElementById('slideshowIndicators');
        indicatorsContainer.innerHTML = '';
        
        series.forEach((_, index) => {
            const dot = document.createElement('div');
            dot.className = `indicator-dot ${index === 0 ? 'active' : ''}`;
            dot.addEventListener('click', () => this.goToSlide(index));
            indicatorsContainer.appendChild(dot);
        });
        
        // Show first slide
        this.showSlide(0);
    }
    
    showSlide(index) {
        if (!this.seriesList || this.seriesList.length === 0) return;
        
        const serie = this.seriesList[index];
        const heroImage = document.getElementById('heroImage');
        const heroTitle = document.getElementById('heroTitle');
        const heroDescription = document.getElementById('heroDescription');
        
        // Update content
        heroImage.src = serie.poster;
        heroTitle.textContent = serie.name;
        heroDescription.textContent = `Discover amazing episodes from ${serie.name}`;
        
        // Update indicators
        document.querySelectorAll('.indicator-dot').forEach((dot, i) => {
            dot.classList.toggle('active', i === index);
        });
        
        this.currentSlideIndex = index;
    }
    
    startSlideshow() {
        // Clear existing interval
        if (this.slideInterval) {
            clearInterval(this.slideInterval);
        }
        
        // Start new interval (7 seconds per slide)
        this.slideInterval = setInterval(() => {
            this.nextSlide();
        }, 7000);
    }
    
    nextSlide() {
        if (!this.seriesList || this.seriesList.length === 0) return;
        
        const nextIndex = (this.currentSlideIndex + 1) % this.seriesList.length;
        this.showSlide(nextIndex);
    }
    
    goToSlide(index) {
        this.showSlide(index);
        this.startSlideshow(); // Restart the timer
    }
    
    playCurrentSeries() {
        if (this.seriesList && this.seriesList[this.currentSlideIndex]) {
            this.openSeriesModal(this.seriesList[this.currentSlideIndex].name);
        }
    }
    
    showCurrentSeriesInfo() {
        if (this.seriesList && this.seriesList[this.currentSlideIndex]) {
            this.openSeriesModal(this.seriesList[this.currentSlideIndex].name);
        }
    }
    
    async openSeriesModal(seriesName) {
        try {
            this.showLoading(true);
            
            // Load seasons
            const seasonsResponse = await fetch(`${this.apiBase}/catalog/series/${encodeURIComponent(seriesName)}`);
            const seasons = await seasonsResponse.json();
            
            this.currentSeries = seriesName;
            this.currentSeason = null;
            
            // Update modal content
            document.getElementById('seriesTitle').textContent = seriesName;
            document.getElementById('seriesSeasonCount').textContent = `${seasons.length} Season${seasons.length !== 1 ? 's' : ''}`;
            document.getElementById('seriesPosterImg').src = `/static/${seriesName.toLowerCase().replace(/\s+/g, '')}.jpg`;
            document.getElementById('seriesPosterImg').onerror = function() { this.src = '/static/default.jpg'; };
            
            // Setup seasons dropdown
            this.populateSeasonsDropdown(seasons);
            
            // Clear episodes list initially
            document.getElementById('episodesList').innerHTML = '<p style="text-align: center; opacity: 0.7; padding: 2rem;">Select a season to view episodes</p>';
            document.getElementById('seriesEpisodeCount').textContent = '';
            
            // Show modal
            document.getElementById('seriesModal').classList.add('active');
            
        } catch (error) {
            this.showToast('Failed to load series details', 'error');
            console.error('Error loading series details:', error);
        } finally {
            this.showLoading(false);
        }
    }
    
    populateSeasonsDropdown(seasons) {
        const dropdown = document.getElementById('seasonSelect');
        dropdown.innerHTML = '<option value="">Choose a season...</option>';
        
        seasons.forEach(season => {
            const option = document.createElement('option');
            option.value = season;
            option.textContent = season;
            dropdown.appendChild(option);
        });
        
        // Reset current season
        this.currentSeason = null;
    }
    
    async switchSeason(seasonName) {
        if (!seasonName) {
            this.currentSeason = null;
            document.getElementById('episodesList').innerHTML = '<p style="text-align: center; opacity: 0.7; padding: 2rem;">Select a season to view episodes</p>';
            document.getElementById('seriesEpisodeCount').textContent = '';
            return;
        }
        
        this.currentSeason = seasonName;
        await this.loadEpisodes(this.currentSeries, seasonName);
    }
    
    async loadEpisodes(seriesName, seasonName) {
        try {
            const response = await fetch(`${this.apiBase}/catalog/series/${encodeURIComponent(seriesName)}/${encodeURIComponent(seasonName)}`);
            const episodes = await response.json();
            
            this.renderEpisodesList(episodes);
            
            // Update episode count
            document.getElementById('seriesEpisodeCount').textContent = `${episodes.length} Episode${episodes.length !== 1 ? 's' : ''}`;
            
        } catch (error) {
            this.showToast('Failed to load episodes', 'error');
            console.error('Error loading episodes:', error);
        }
    }
    
    renderEpisodesList(episodes) {
        const list = document.getElementById('episodesList');
        list.innerHTML = '';
        
        episodes.forEach((episode, index) => {
            const item = document.createElement('div');
            item.className = 'episode-item';
            
            const statusClass = episode.downloaded ? 'downloaded' : 
                               episode.downloading ? 'downloading' : '';
            const statusText = episode.downloaded ? 'Downloaded' : 
                              episode.downloading ? 'Downloading...' : 'Not Downloaded';
            
            item.innerHTML = `
                <div class="episode-number">${index + 1}</div>
                <div class="episode-details">
                    <div class="episode-title">${episode.title}</div>
                    <div class="episode-description">${episode.description || 'No description available'}</div>
                </div>
                <div class="episode-actions">
                    <button class="episode-download-btn" data-msg-id="${episode.msg_id}" ${episode.downloaded ? 'disabled' : ''}>
                        ${episode.downloaded ? '✓ Downloaded' : '⬇ Download'}
                    </button>
                    <div class="episode-status">
                        <div class="status-indicator ${statusClass}"></div>
                        <span>${statusText}</span>
                    </div>
                </div>
            `;
            
            // Play episode on click (but not on download button)
            item.addEventListener('click', (e) => {
                if (!e.target.classList.contains('episode-download-btn')) {
                    this.playEpisode(episode);
                }
            });
            
            // Download button
            const downloadBtn = item.querySelector('.episode-download-btn');
            if (!episode.downloaded) {
                downloadBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    this.downloadEpisode(episode);
                });
            }
            
            list.appendChild(item);
        });
    }
    
    async downloadEpisode(episode) {
        try {
            // Find episode URL from the episode object
            if (!episode.url) {
                this.showToast('Episode URL not found', 'error');
                return;
            }
            
            this.showToast('Starting download...', 'info');
            
            const response = await fetch(`${this.apiBase}/download?url=${encodeURIComponent(episode.url)}`);
            const result = await response.json();
            
            if (result.status === 'queued' || result.status === 'already_queued') {
                this.showToast(`Episode queued for download (${result.priority} priority)`, 'success');
            } else if (result.status === 'already_uploaded') {
                this.showToast('Episode already downloaded', 'info');
            } else {
                this.showToast('Download failed', 'error');
            }
            
        } catch (error) {
            this.showToast('Failed to start download', 'error');
            console.error('Error downloading episode:', error);
        }
    }
    
    async downloadCurrentSeason() {
        if (!this.currentSeries || !this.currentSeason) {
            this.showToast('Please select a season first', 'warning');
            return;
        }
        
        try {
            this.showToast('Starting season download...', 'info');
            
            const response = await fetch(`${this.apiBase}/download/season?series_name=${encodeURIComponent(this.currentSeries)}&season_name=${encodeURIComponent(this.currentSeason)}`, {
                method: 'POST'
            });
            
            const result = await response.json();
            
            if (result.status === 'queued') {
                this.showToast(`Season queued: ${result.queued_count} episodes for download`, 'success');
            } else if (result.status === 'already_downloaded') {
                this.showToast('All episodes already downloaded', 'info');
            } else {
                this.showToast('Season download failed', 'error');
            }
            
        } catch (error) {
            this.showToast('Failed to start season download', 'error');
            console.error('Error downloading season:', error);
        }
    }
    
    async downloadAllSeries() {
        if (!this.currentSeries) {
            this.showToast('No series selected', 'warning');
            return;
        }
        
        try {
            // Get all seasons for the series
            const seasonsResponse = await fetch(`${this.apiBase}/catalog/series/${encodeURIComponent(this.currentSeries)}`);
            const seasons = await seasonsResponse.json();
            
            this.showToast(`Starting download for all ${seasons.length} seasons...`, 'info');
            
            let totalQueued = 0;
            for (const season of seasons) {
                try {
                    const response = await fetch(`${this.apiBase}/download/season?series_name=${encodeURIComponent(this.currentSeries)}&season_name=${encodeURIComponent(season)}`, {
                        method: 'POST'
                    });
                    
                    const result = await response.json();
                    if (result.status === 'queued') {
                        totalQueued += result.queued_count;
                    }
                    
                    // Small delay between season requests
                    await new Promise(resolve => setTimeout(resolve, 500));
                    
                } catch (error) {
                    console.error(`Error downloading season ${season}:`, error);
                }
            }
            
            if (totalQueued > 0) {
                this.showToast(`All series queued: ${totalQueued} episodes for download`, 'success');
            } else {
                this.showToast('All episodes already downloaded or failed to queue', 'info');
            }
            
        } catch (error) {
            this.showToast('Failed to start series download', 'error');
            console.error('Error downloading all series:', error);
        }
    }
    
    async playEpisode(episode) {
        try {
            this.currentEpisode = episode;
            this.closeSeriesModal();
            this.openVideoModal();
            
            // Update video info
            document.getElementById('videoTitle').textContent = episode.title;
            document.getElementById('videoDescription').textContent = episode.description || '';
            
            // Show loading spinner
            if (this.player && this.player.showLoading) {
                this.player.showLoading();
            }
            
            // Check if episode is downloaded
            if (episode.downloaded) {
                // Use existing streaming endpoint for downloaded episodes
                this.loadVideoSource(`${this.apiBase}/stream_local/${episode.msg_id}`, episode.title);
            } else {
                // For non-downloaded episodes, check download progress first
                this.showToast('Starting download and streaming...', 'info');
                
                // Start download and monitor progress
                await this.monitorStreamProgress(episode.msg_id, episode.title);
            }
        } catch (error) {
            this.showToast('Failed to start episode', 'error');
            console.error('Error playing episode:', error);
        }
    }

    async monitorStreamProgress(msg_id, title) {
        const minBufferPercentage = 3; // Wait for 3% buffer before playing
        let attempts = 0;
        let lastPercentage = 0;
        
        // Show initial loading
        this.showToast('Starting download and buffering...', 'info');
        
        // IMPORTANT: First trigger the download by hitting the streaming endpoint
        try {
            // This will trigger the download on the server side
            await fetch(`${this.apiBase}/stream_mobile/${msg_id}`, { method: 'HEAD' });
            
            // Give the server a moment to start the download
            await new Promise(resolve => setTimeout(resolve, 2000));
        } catch (error) {
            console.log('Preflight request completed');
            // Ignore errors, we just want to trigger the download
        }
        
        const checkProgress = async () => {
            try {
                // Use the real progress endpoint to check download status
                const response = await fetch(`${this.apiBase}/download/real_progress/${msg_id}`);
                const progress = await response.json();
                
                if (!progress.downloading) {
                    attempts++;
                    
                    if (attempts >= 5) {
                        // After several attempts, try to trigger download directly
                        this.showToast('Download not starting, trying alternate method...', 'warning');
                        
                        try {
                            // Try to find episode URL and trigger download directly
                            const catalogResponse = await fetch(`${this.apiBase}/catalog/episode/${msg_id}`);
                            const episodeInfo = await catalogResponse.json();
                            
                            if (episodeInfo && episodeInfo.url) {
                                await fetch(`${this.apiBase}/download?url=${encodeURIComponent(episodeInfo.url)}`);
                                this.showToast('Download triggered, please wait...', 'info');
                                
                                // Give it time to start
                                await new Promise(resolve => setTimeout(resolve, 3000));
                            }
                        } catch (err) {
                            console.error('Failed to trigger download directly:', err);
                        }
                    }
                    
                    if (attempts < 15) {
                        // Keep checking for a while
                        this.showToast('Waiting for download to start...', 'info');
                        setTimeout(checkProgress, 2000);
                    } else {
                        this.showToast('Download failed to start. Please try again later.', 'error');
                    }
                    return;
                }
                
                const percentage = progress.percentage || 0;
                
                // Show progress updates (but not too frequently)
                if (Math.floor(percentage) > Math.floor(lastPercentage)) {
                    lastPercentage = percentage;
                    this.showToast(`Buffering: ${percentage.toFixed(1)}%`, 'info');
                }
                
                // Check if we have enough buffer to start playing
                if (percentage >= minBufferPercentage) {
                    this.showToast(`Starting playback (${percentage.toFixed(1)}% buffered)`, 'success');
                    this.loadVideoSource(`${this.apiBase}/stream_mobile/${msg_id}`, title);
                    return;
                }
                
                // Continue checking
                attempts++;
                setTimeout(checkProgress, 1000); // Check every second
                
            } catch (error) {
                console.error('Error checking download progress:', error);
                if (attempts < 30) { // Try for 30 seconds max
                    setTimeout(checkProgress, 2000); // Retry with longer timeout
                } else {
                    this.showToast('Buffering failed. Please try again later.', 'error');
                }
            }
    };
    
    // Start checking
    checkProgress();
    }
    
    loadVideoSource(url, title) {
        const video = document.getElementById('videoPlayer');
        const source = video ? video.querySelector('source') : null;
        
        if (!video || !source) {
            this.showToast('Video player not found', 'error');
            return;
        }
        
        // Show loading
        try {
            if (this.player && this.player.showLoading) {
                this.player.showLoading();
            }
        } catch (error) {
            console.error('Error showing loading:', error);
        }

        // Add error event listeners to diagnose problems
        video.addEventListener('error', (e) => {
            console.error('Video error:', video.error);
            this.showToast(`Video error: ${video.error ? video.error.message : 'Unknown error'}`, 'error');
        }, {once: true});
        
        source.addEventListener('error', (e) => {
            console.error('Source error:', e);
            this.showToast('Source failed to load', 'error');
        }, {once: true});
        
        // Set video source
        source.src = url;
        source.type = 'video/mp4';
        video.load();

        // Monitor loading states
        video.addEventListener('loadstart', () => console.log('Video loadstart'), {once: true});
        video.addEventListener('loadeddata', () => console.log('Video loadeddata'), {once: true});
        video.addEventListener('canplay', () => {
            console.log('Video canplay - attempting to play');
            video.play().catch(err => {
                console.error('Play error:', err);
                this.showToast(`Play error: ${err}`, 'error');
            });
        }, {once: true});
        
        // Update title
        const videoTitle = document.getElementById('videoTitle');
        if (videoTitle && title) {
            videoTitle.textContent = title;
        }
        
        // Auto-play when ready
        video.addEventListener('canplay', () => {
            video.play().catch(console.error);
        }, { once: true });
    }

    
    openVideoModal() {
        document.getElementById('videoModal').classList.add('active');
        document.body.style.overflow = 'hidden';
        
        // Pause slideshow when video is playing
        if (this.slideInterval) {
            clearInterval(this.slideInterval);
        }
    }
    
    closeVideoModal() {
        const modal = document.getElementById('videoModal');
        modal.classList.remove('active');
        document.body.style.overflow = '';
        
        // Pause video
        const video = document.getElementById('videoPlayer');
        video.pause();
        video.src = '';
        
        // Resume slideshow
        this.startSlideshow();
    }
    
    closeSeriesModal() {
        document.getElementById('seriesModal').classList.remove('active');
        
        // Reset dropdown
        document.getElementById('seasonSelect').value = '';
        this.currentSeason = null;
    }
    
    closeAllModals() {
        this.closeSeriesModal();
        this.closeVideoModal();
    }
    
    setupVideoPlayer() {
        this.player = new NetflixVideoPlayer();
    }
    
    showLoading(show) {
        const overlay = document.getElementById('loadingOverlay');
        if (show) {
            overlay.classList.add('active');
        } else {
            overlay.classList.remove('active');
        }
    }
    
    showToast(message, type = 'info') {
        const container = document.getElementById('toastContainer');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.textContent = message;
        
        container.appendChild(toast);
        
        // Show toast
        setTimeout(() => toast.classList.add('show'), 100);
        
        // Remove toast after 4 seconds
        setTimeout(() => {
            toast.classList.remove('show');
            setTimeout(() => {
                if (container.contains(toast)) {
                    container.removeChild(toast);
                }
            }, 300);
        }, 4000);
    }
}

// Enhanced Video Player Class (same as before)
class NetflixVideoPlayer {
    constructor() {
        // Core video elements with null checks
        this.video = document.getElementById('videoPlayer');
        this.controlsOverlay = document.getElementById('controlsOverlay');
        this.playPauseBtn = document.getElementById('playPauseBtn');
        this.progressBar = document.getElementById('progressBar');
        this.progressFilled = document.getElementById('progressFilled');
        this.progressBuffer = document.getElementById('progressBuffer');
        this.progressHandle = document.getElementById('progressHandle');
        this.volumeSlider = document.getElementById('volumeSlider');
        this.timeDisplay = document.getElementById('timeDisplay');
        this.loadingSpinner = document.getElementById('loadingSpinner');
        this.nextEpisodeBtn = document.getElementById('nextEpisodeBtn');
        
        // Audio tracks elements with null checks
        this.audioTracksBtn = document.getElementById('audioTracksBtn');
        this.audioTracksDrawer = document.getElementById('audioTracksDrawer');
        this.audioTracksOverlay = document.getElementById('audioTracksOverlay');
        this.audioTracksList = document.getElementById('audioTracksList');
        this.audioTracksListMobile = document.getElementById('audioTracksListMobile'); // Fixed ID
        this.closeAudioDrawer = document.getElementById('closeAudioDrawer');
        this.closeAudioOverlay = document.getElementById('closeAudioOverlay');
        
        // Check if critical elements exist
        if (!this.video) {
            console.error('Critical error: videoPlayer element not found!');
            return;
        }
        
        if (!this.controlsOverlay) {
            console.error('Critical error: controlsOverlay element not found!');
            return;
        }
        
        this.controlsTimeout = null;
        this.isDragging = false;
        this.audioTracksVisible = false;
        this.currentAudioTrack = 0;
        
        this.initializePlayer();
        this.setupEventListeners();
    }
    
    initializePlayer() {
        if (!this.video) return;
        
        this.video.controls = false;
        if (this.volumeSlider) {
            this.video.volume = this.volumeSlider.value / 100;
        }
        this.showLoading();
    }
    
    setupEventListeners() {
        if (!this.video || !this.controlsOverlay) {
            console.error('Cannot setup event listeners: required elements not found');
            return;
        }
        
        // Video events with null checks
        this.video.addEventListener('loadstart', () => this.showLoading());
        this.video.addEventListener('canplay', () => this.hideLoading());
        this.video.addEventListener('waiting', () => this.showLoading());
        this.video.addEventListener('playing', () => this.hideLoading());
        this.video.addEventListener('timeupdate', () => this.updateProgress());
        this.video.addEventListener('progress', () => this.updateBuffer());
        this.video.addEventListener('ended', () => this.onVideoEnded());
        this.video.addEventListener('loadedmetadata', () => this.loadAudioTracks());
        
        // Play/Pause with null checks
        if (this.playPauseBtn) {
            this.playPauseBtn.addEventListener('click', () => this.togglePlayPause());
        }
        this.video.addEventListener('click', () => this.togglePlayPause());
        
        // Progress bar with null checks
        if (this.progressBar) {
            this.progressBar.addEventListener('mousedown', (e) => this.startDragging(e));
        }
        document.addEventListener('mousemove', (e) => this.onDragging(e));
        document.addEventListener('mouseup', () => this.stopDragging());
        
        // Volume control with null checks
        if (this.volumeSlider) {
            this.volumeSlider.addEventListener('input', (e) => this.setVolume(e.target.value));
        }
        
        // Controls visibility
        this.controlsOverlay.addEventListener('mousemove', () => this.showControls());
        this.controlsOverlay.addEventListener('mouseleave', () => this.hideControls());
        
        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => this.handleKeyboard(e));
        
        // Fullscreen with null checks
        const fullscreenBtn = document.getElementById('fullscreenBtn');
        if (fullscreenBtn) {
            fullscreenBtn.addEventListener('click', () => this.toggleFullscreen());
        }
        
        // Double click for fullscreen
        this.video.addEventListener('dblclick', () => this.toggleFullscreen());
        
        // Next episode with null checks
        if (this.nextEpisodeBtn) {
            this.nextEpisodeBtn.addEventListener('click', () => this.playNextEpisode());
        }
        
        // Audio tracks with null checks
        if (this.audioTracksBtn) {
            this.audioTracksBtn.addEventListener('click', () => this.toggleAudioTracksMenu());
        }
        if (this.closeAudioDrawer) {
            this.closeAudioDrawer.addEventListener('click', () => this.hideAudioTracksMenu());
        }
        if (this.closeAudioOverlay) {
            this.closeAudioOverlay.addEventListener('click', () => this.hideAudioTracksMenu());
        }
        
        // Close audio tracks when clicking outside
        if (this.audioTracksOverlay) {
            this.audioTracksOverlay.addEventListener('click', (e) => {
                if (e.target === this.audioTracksOverlay) {
                    this.hideAudioTracksMenu();
                }
            });
        }
    }
    
    // All your existing methods remain the same, but add null checks in critical ones:
    
    showLoading() {
        if (this.loadingSpinner) {
            this.loadingSpinner.classList.remove('hidden');
        }
    }
    
    hideLoading() {
        if (this.loadingSpinner) {
            this.loadingSpinner.classList.add('hidden');
        }
    }
    
    togglePlayPause() {
        if (!this.video) return;
        
        if (this.video.paused) {
            this.video.play();
            this.updatePlayPauseIcon(false);
        } else {
            this.video.pause();
            this.updatePlayPauseIcon(true);
        }
    }
    
    updatePlayPauseIcon(paused) {
        if (!this.playPauseBtn) return;
        
        const playIcon = this.playPauseBtn.querySelector('.play-icon');
        const pauseIcon = this.playPauseBtn.querySelector('.pause-icon');
        
        if (playIcon && pauseIcon) {
            if (paused) {
                playIcon.classList.remove('hidden');
                pauseIcon.classList.add('hidden');
            } else {
                playIcon.classList.add('hidden');
                pauseIcon.classList.remove('hidden');
            }
        }
    }
    
    updateProgress() {
        if (!this.isDragging && this.video && this.video.duration && this.progressFilled && this.progressHandle) {
            const progress = (this.video.currentTime / this.video.duration) * 100;
            this.progressFilled.style.width = progress + '%';
            this.progressHandle.style.left = progress + '%';
        }
        
        this.updateTimeDisplay();
    }
    
    updateBuffer() {
        if (this.video && this.video.buffered.length > 0 && this.progressBuffer) {
            const buffered = (this.video.buffered.end(this.video.buffered.length - 1) / this.video.duration) * 100;
            this.progressBuffer.style.width = buffered + '%';
        }
    }
    
    updateTimeDisplay() {
        if (!this.timeDisplay || !this.video) return;
        
        const current = this.formatTime(this.video.currentTime);
        const duration = this.formatTime(this.video.duration);
        this.timeDisplay.textContent = `${current} / ${duration}`;
    }
    
    formatTime(seconds) {
        if (isNaN(seconds)) return '0:00';
        
        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    }
    
    startDragging(e) {
        this.isDragging = true;
        this.updateProgressFromEvent(e);
    }
    
    onDragging(e) {
        if (this.isDragging) {
            this.updateProgressFromEvent(e);
        }
    }
    
    stopDragging() {
        this.isDragging = false;
    }
    
    updateProgressFromEvent(e) {
        if (!this.progressBar || !this.video || !this.progressFilled || !this.progressHandle) return;
        
        const rect = this.progressBar.getBoundingClientRect();
        const clickX = e.clientX - rect.left;
        const progress = Math.max(0, Math.min(1, clickX / rect.width));
        
        const newTime = progress * this.video.duration;
        this.video.currentTime = newTime;
        
        this.progressFilled.style.width = (progress * 100) + '%';
        this.progressHandle.style.left = (progress * 100) + '%';
    }
    
    setVolume(value) {
        if (this.video) {
            this.video.volume = value / 100;
        }
    }
    
    showControls() {
        if (this.controlsOverlay) {
            this.controlsOverlay.classList.add('show');
        }
        this.resetControlsTimeout();
    }
    
    hideControls() {
        if (!this.audioTracksVisible && this.controlsOverlay) {
            this.controlsOverlay.classList.remove('show');
        }
    }
    
    resetControlsTimeout() {
        clearTimeout(this.controlsTimeout);
        this.controlsTimeout = setTimeout(() => {
            if (this.video && !this.video.paused && !this.audioTracksVisible) {
                this.hideControls();
            }
        }, 3000);
    }
    
    // Audio Tracks Methods with null checks
    loadAudioTracks() {
        try {
            if (!this.video) return;
            
            const audioTracks = this.video.audioTracks;
            
            if (audioTracks && audioTracks.length > 1) {
                if (this.audioTracksBtn) {
                    this.audioTracksBtn.style.display = 'block';
                }
                this.populateAudioTracksList(audioTracks);
            } else {
                if (this.audioTracksBtn) {
                    this.audioTracksBtn.style.display = 'none';
                }
                this.showNoTracksMessage();
            }
        } catch (error) {
            console.log('Audio tracks not supported or unavailable');
            if (this.audioTracksBtn) {
                this.audioTracksBtn.style.display = 'none';
            }
            this.showNoTracksMessage();
        }
    }
    
    populateAudioTracksList(audioTracks) {
        // Clear existing tracks
        if (this.audioTracksList) {
            this.audioTracksList.innerHTML = '';
        }
        if (this.audioTracksListMobile) {
            this.audioTracksListMobile.innerHTML = '';
        }
        
        // Add tracks to both desktop and mobile lists
        for (let i = 0; i < audioTracks.length; i++) {
            const track = audioTracks[i];
            
            if (this.audioTracksList) {
                const trackItem = this.createAudioTrackItem(track, i);
                this.audioTracksList.appendChild(trackItem);
            }
            
            if (this.audioTracksListMobile) {
                const trackItemMobile = this.createAudioTrackItem(track, i);
                this.audioTracksListMobile.appendChild(trackItemMobile);
            }
        }
    }
    
    createAudioTrackItem(track, index) {
        const trackItem = document.createElement('div');
        trackItem.className = `audio-track-item ${track.enabled ? 'active' : ''}`;
        trackItem.dataset.trackIndex = index;
        
        // Get track info
        const language = track.language || track.label || `Track ${index + 1}`;
        const kind = track.kind || 'audio';
        const label = track.label || '';
        
        trackItem.innerHTML = `
            <div class="track-indicator"></div>
            <div class="track-info">
                <div class="track-language">${language}</div>
                <div class="track-details">${label || kind}</div>
            </div>
        `;
        
        trackItem.addEventListener('click', () => {
            this.selectAudioTrack(index);
        });
        
        return trackItem;
    }
    
    showNoTracksMessage() {
        const noTracksHTML = `
            <div class="no-tracks-message">
                <p>No additional audio tracks available</p>
            </div>
        `;
        
        if (this.audioTracksList) {
            this.audioTracksList.innerHTML = noTracksHTML;
        }
        if (this.audioTracksListMobile) {
            this.audioTracksListMobile.innerHTML = noTracksHTML;
        }
    }
    
    toggleAudioTracksMenu() {
        if (this.audioTracksVisible) {
            this.hideAudioTracksMenu();
        } else {
            this.showAudioTracksMenu();
        }
    }
    
    showAudioTracksMenu() {
        this.audioTracksVisible = true;
        if (this.audioTracksBtn) {
            this.audioTracksBtn.classList.add('active');
        }
        
        // Show appropriate menu based on screen size
        if (window.innerWidth <= 768) {
            if (this.audioTracksOverlay) {
                this.audioTracksOverlay.classList.add('active');
            }
        } else {
            if (this.audioTracksDrawer) {
                this.audioTracksDrawer.classList.add('active');
            }
        }
        
        // Keep controls visible
        this.showControls();
        
        // Load tracks if video is ready
        if (this.video && this.video.readyState >= 1) {
            this.loadAudioTracks();
        }
    }
    
    hideAudioTracksMenu() {
        this.audioTracksVisible = false;
        if (this.audioTracksBtn) {
            this.audioTracksBtn.classList.remove('active');
        }
        if (this.audioTracksDrawer) {
            this.audioTracksDrawer.classList.remove('active');
        }
        if (this.audioTracksOverlay) {
            this.audioTracksOverlay.classList.remove('active');
        }
        
        // Resume normal controls behavior
        this.resetControlsTimeout();
    }
    
    selectAudioTrack(trackIndex) {
        try {
            if (!this.video) return;
            
            const audioTracks = this.video.audioTracks;
            
            if (audioTracks && trackIndex < audioTracks.length) {
                // Disable all tracks
                for (let i = 0; i < audioTracks.length; i++) {
                    audioTracks[i].enabled = false;
                }
                
                // Enable selected track
                audioTracks[trackIndex].enabled = true;
                this.currentAudioTrack = trackIndex;
                
                // Update UI
                this.updateAudioTrackSelection(trackIndex);
                
                // Show success message
                const trackName = audioTracks[trackIndex].language || 
                                audioTracks[trackIndex].label || 
                                `Track ${trackIndex + 1}`;
                if (window.app) {
                    window.app.showToast(`Audio track changed to: ${trackName}`, 'success');
                }
                
                // Close menu after selection
                setTimeout(() => {
                    this.hideAudioTracksMenu();
                }, 500);
            }
        } catch (error) {
            console.error('Error selecting audio track:', error);
            if (window.app) {
                window.app.showToast('Failed to change audio track', 'error');
            }
        }
    }
    
    updateAudioTrackSelection(selectedIndex) {
        // Update both desktop and mobile lists
        const allTrackItems = [];
        
        if (this.audioTracksList) {
            allTrackItems.push(...this.audioTracksList.querySelectorAll('.audio-track-item'));
        }
        if (this.audioTracksListMobile) {
            allTrackItems.push(...this.audioTracksListMobile.querySelectorAll('.audio-track-item'));
        }
        
        allTrackItems.forEach((item) => {
            const actualIndex = parseInt(item.dataset.trackIndex);
            if (actualIndex === selectedIndex) {
                item.classList.add('active', 'selected');
                setTimeout(() => item.classList.remove('selected'), 300);
            } else {
                item.classList.remove('active');
            }
        });
    }
    
    handleKeyboard(e) {
        // Only handle keyboard events when video modal is open
        if (!document.getElementById('videoModal') || !document.getElementById('videoModal').classList.contains('active')) {
            return;
        }
        
        switch(e.code) {
            case 'Space':
                e.preventDefault();
                this.togglePlayPause();
                break;
            case 'ArrowLeft':
                e.preventDefault();
                if (this.video) {
                    this.video.currentTime -= 10;
                }
                break;
            case 'ArrowRight':
                e.preventDefault();
                if (this.video) {
                    this.video.currentTime += 10;
                }
                break;
            case 'ArrowUp':
                e.preventDefault();
                if (this.video && this.volumeSlider) {
                    this.video.volume = Math.min(1, this.video.volume + 0.1);
                    this.volumeSlider.value = this.video.volume * 100;
                }
                break;
            case 'ArrowDown':
                e.preventDefault();
                if (this.video && this.volumeSlider) {
                    this.video.volume = Math.max(0, this.video.volume - 0.1);
                    this.volumeSlider.value = this.video.volume * 100;
                }
                break;
            case 'KeyF':
                e.preventDefault();
                this.toggleFullscreen();
                break;
            case 'KeyA':
                e.preventDefault();
                this.toggleAudioTracksMenu();
                break;
            case 'Escape':
                e.preventDefault();
                if (this.audioTracksVisible) {
                    this.hideAudioTracksMenu();
                }
                break;
        }
    }
    
    toggleFullscreen() {
        if (!document.fullscreenElement) {
            const videoModal = document.getElementById('videoModal');
            if (videoModal) {
                videoModal.requestFullscreen();
            }
        } else {
            document.exitFullscreen();
        }
    }
    
    onVideoEnded() {
        this.updatePlayPauseIcon(true);
        this.showControls();
        if (window.app) {
            window.app.showToast('Episode ended', 'info');
        }
    }
    
    playNextEpisode() {
        if (window.app) {
            window.app.showToast('Next episode feature coming soon!', 'info');
        }
    }
    
    changeSource(newSrc, title = '') {
        if (!this.video) return;
        
        this.showLoading();
        this.video.src = newSrc;
        this.video.load();
        
        // Reset audio tracks
        this.hideAudioTracksMenu();
        this.currentAudioTrack = 0;
        
        if (title) {
            const videoTitle = document.getElementById('videoTitle');
            if (videoTitle) {
                videoTitle.textContent = title;
            }
        }
    }
}


// Initialize the app when page loads
let app;
document.addEventListener('DOMContentLoaded', () => {
    app = new StreamFlixApp();
});
