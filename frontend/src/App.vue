<script setup>
import { ref, onMounted, onUnmounted, computed, watch } from 'vue'
import L from 'leaflet'

const API_BASE = '' // relative path since we serve it from FastAPI, or http://localhost:8000 in dev

// State variables
const drawMode = ref(null) // null, 'circle', 'polygon'
const drawHelpText = ref('')
const timeoutHours = ref(2.0)
const activeTab = ref('strikes')
const testingWebhook = ref(false)
const localType = ref(null)
const localCoordinates = ref(null)

const savingConfig = ref(false)
const configStatus = ref({
  xweather_configured: false,
  discord_configured: false,
  xweather_api_key_masked: '',
  discord_webhook_masked: ''
})
const configForm = ref({
  xweather_api_key: '',
  discord_webhook: ''
})

const status = ref({
  active: false,
  start_time: null,
  end_time: null,
  timeout_hours: null,
  type: null,
  coordinates: null,
  time_remaining_seconds: null,
  checks_count: 0,
  detections_count: 0,
  recent_strikes: [],
  logs: []
})

// Leaflet map instance and layers
let map = null
let tileLayer = null
let drawLayer = null // Layer group for drawn shapes
let strikeGroup = null // Layer group for detected strikes

// Temporary draw objects
let tempPoints = []
let tempMarkers = []
let tempShape = null
let tempGuideLine = null

// Stored references for strikes
const strikeMarkers = {}

// Computeds
const isConfigured = computed(() => {
  return configStatus.value.xweather_configured && configStatus.value.discord_configured
})

const hasDrawnShape = computed(() => {
  return localCoordinates.value !== null
})

const shapeDetailsText = computed(() => {
  if (!localCoordinates.value) return ''
  if (localType.value === 'circle') {
    const radiusKm = localCoordinates.value.radius / 1000.0
    return `Circle: ${radiusKm.toFixed(2)} km radius`
  } else {
    const pointsCount = localCoordinates.value.polygon?.length || 0
    return `Polygon: ${pointsCount} vertices`
  }
})

const timeRemainingText = computed(() => {
  const seconds = status.value.time_remaining_seconds
  if (seconds === null || seconds === undefined) return '--:--'
  if (seconds <= 0) return 'Expired'
  const hrs = Math.floor(seconds / 3600)
  const mins = Math.floor((seconds % 3600) / 60)
  const secs = Math.floor(seconds % 60)
  return `${hrs.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`
})

const sortedStrikes = computed(() => {
  // Sort latest strikes first
  return status.value.recent_strikes.slice().reverse()
})

// Methods
const fetchStatus = async () => {
  try {
    const resp = await fetch(`${API_BASE}/api/status`)
    if (resp.ok) {
      const data = await resp.json()
      status.value = data
      
      // If the backend has an active session, sync local shape with the backend's active shape
      if (data.active) {
        localType.value = data.type
        localCoordinates.value = data.coordinates
      }
      
      // Update map features based on status
      updateMapFromStatus()
    }
  } catch (err) {
    console.error('Failed to fetch status:', err)
  }
}

// Map initialization
onMounted(() => {
  // Setup Leaflet map
  map = L.map('map', {
    zoomControl: false
  }).setView([39.8283, -98.5795], 4) // Center of USA default fallback

  // Custom Zoom Control on topright
  L.control.zoom({ position: 'topright' }).addTo(map)

  // CartoDB Dark Matter tile layer
  tileLayer = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: 'abcd',
    maxZoom: 20
  }).addTo(map)

  drawLayer = L.layerGroup().addTo(map)
  strikeGroup = L.layerGroup().addTo(map)

  // Listen to clicks on map for drawing
  map.on('click', handleMapClick)
  map.on('mousemove', handleMapMouseMove)

  // Try browser geolocation to center on user location
  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(
      (position) => {
        // Only center on user if backend monitoring is not active
        if (!status.value.active) {
          const lat = position.coords.latitude
          const lon = position.coords.longitude
          map.setView([lat, lon], 10)
          
          // Place a clean location marker at their coordinates
          L.circleMarker([lat, lon], {
            radius: 6,
            color: 'var(--ace-secondary)',
            fillColor: 'var(--ace-secondary)',
            fillOpacity: 1.0,
            weight: 2
          }).addTo(map).bindPopup('You are here')
        }
      },
      (error) => {
        console.warn('Geolocation failed or permission denied:', error)
      }
    )
  }

  // Start polling status
  fetchConfig()
  fetchStatus()
  const interval = setInterval(fetchStatus, 3000)
  onUnmounted(() => {
    clearInterval(interval)
  })
})

const handleMapClick = (e) => {
  if (drawMode.value === 'circle') {
    handleCircleDrawClick(e)
  } else if (drawMode.value === 'polygon') {
    handlePolygonDrawClick(e)
  }
}

const handleMapMouseMove = (e) => {
  if (drawMode.value === 'circle' && tempPoints.length === 1) {
    const center = L.latLng(tempPoints[0])
    const currentPoint = e.latlng
    const radius = center.distanceTo(currentPoint)
    
    if (tempShape) {
      tempShape.setRadius(radius)
    } else {
      tempShape = L.circle(center, {
        radius: radius,
        color: 'var(--ace-secondary)',
        dashArray: '5, 5',
        fillColor: 'var(--ace-secondary)',
        fillOpacity: 0.1,
        weight: 2
      }).addTo(drawLayer)
    }
    
    const radiusKm = radius / 1000.0
    drawHelpText.value = `Radius: ${radiusKm.toFixed(2)} km. Click again to set.`
  } else if (drawMode.value === 'polygon' && tempPoints.length > 0) {
    const lastPoint = tempPoints[tempPoints.length - 1]
    const currentPoint = [e.latlng.lat, e.latlng.lng]
    
    if (tempGuideLine) {
      tempGuideLine.setLatLngs([lastPoint, currentPoint])
    } else {
      tempGuideLine = L.polyline([lastPoint, currentPoint], {
        color: 'var(--ace-secondary)',
        dashArray: '5, 5',
        weight: 2
      }).addTo(drawLayer)
    }
  }
}

// Circle drawing logic
const startDrawingCircle = () => {
  cancelDrawing()
  drawMode.value = 'circle'
  drawHelpText.value = 'Click on the map to set the center of the detection circle.'
  tempPoints = []
}

const handleCircleDrawClick = (e) => {
  if (tempPoints.length === 0) {
    // Set center
    const center = [e.latlng.lat, e.latlng.lng]
    tempPoints.push(center)
    
    // Add center marker
    const centerMarker = L.marker(e.latlng, {
      icon: L.divIcon({
        className: 'custom-draw-marker',
        html: '<div style="background: var(--ace-secondary); width: 12px; height: 12px; border-radius: 50%; border: 2px solid white; box-shadow: 0 0 4px rgba(0,0,0,0.5);"></div>',
        iconSize: [12, 12],
        iconAnchor: [6, 6]
      })
    }).addTo(drawLayer)
    
    tempMarkers.push(centerMarker)
    drawHelpText.value = 'Move the mouse and click to set the radius.'
  } else if (tempPoints.length === 1) {
    // Set radius
    const center = L.latLng(tempPoints[0])
    const radius = center.distanceTo(e.latlng)
    
    // Save to local shape coordinates
    localType.value = 'circle'
    localCoordinates.value = {
      center: tempPoints[0],
      radius: radius
    }
    
    finalizeDraw()
  }
}

// Polygon drawing logic
const startDrawingPolygon = () => {
  cancelDrawing()
  drawMode.value = 'polygon'
  drawHelpText.value = 'Click on the map to place vertices of the polygon. Place at least 3 points.'
  tempPoints = []
}

const handlePolygonDrawClick = (e) => {
  const currentPoint = [e.latlng.lat, e.latlng.lng]
  tempPoints.push(currentPoint)
  
  // Add marker for vertex
  const vertexMarker = L.marker(e.latlng, {
    icon: L.divIcon({
      className: 'custom-draw-marker',
      html: `<div style="background: ${tempPoints.length === 1 ? 'var(--ace-primary)' : 'var(--ace-secondary)'}; width: 12px; height: 12px; border-radius: 50%; border: 2px solid white; box-shadow: 0 0 4px rgba(0,0,0,0.5);"></div>`,
      iconSize: [12, 12],
      iconAnchor: [6, 6]
    })
  }).addTo(drawLayer)
  
  // Click first marker to complete polygon
  if (tempPoints.length === 1) {
    vertexMarker.on('click', (ev) => {
      L.DomEvent.stopPropagation(ev)
      completePolygon()
    })
  }
  
  tempMarkers.push(vertexMarker)
  
  // Update polyline
  if (tempShape) {
    tempShape.setLatLngs(tempPoints)
  } else {
    tempShape = L.polyline(tempPoints, {
      color: 'var(--ace-secondary)',
      weight: 2
    }).addTo(drawLayer)
  }
  
  if (tempPoints.length >= 3) {
    drawHelpText.value = `Placed ${tempPoints.length} points. Click the first point (lime) or click complete to finish.`
  } else {
    drawHelpText.value = `Placed ${tempPoints.length} point(s). Click map for next point.`
  }
}

const completePolygon = () => {
  if (tempPoints.length < 3) {
    alert('Please place at least 3 points to complete the polygon.')
    return
  }
  
  localType.value = 'polygon'
  localCoordinates.value = {
    polygon: tempPoints
  }
  
  finalizeDraw()
}

// Finish draw operations
const finalizeDraw = () => {
  if (tempGuideLine) {
    drawLayer.removeLayer(tempGuideLine)
    tempGuideLine = null
  }
  
  drawMode.value = null
  drawHelpText.value = ''
  
  updateMapFromStatus()
}

const cancelDrawing = () => {
  drawMode.value = null
  drawHelpText.value = ''
  tempPoints = []
  
  tempMarkers.forEach(m => drawLayer.removeLayer(m))
  tempMarkers = []
  
  if (tempShape) {
    drawLayer.removeLayer(tempShape)
    tempShape = null
  }
  
  if (tempGuideLine) {
    drawLayer.removeLayer(tempGuideLine)
    tempGuideLine = null
  }
}

const clearDrawnShape = () => {
  cancelDrawing()
  localType.value = null
  localCoordinates.value = null
  drawLayer.clearLayers()
  if (status.value.active) {
    stopMonitoring()
  }
}

// Render shapes and markers on map from status
let renderedActiveShape = null
let hasFittedBounds = false

const updateMapFromStatus = () => {
  if (!map) return

  // 1. Render active/defined area
  if (renderedActiveShape) {
    drawLayer.removeLayer(renderedActiveShape)
    renderedActiveShape = null
  }

  // Clear drawing markers if not drawing
  if (!drawMode.value) {
    drawLayer.clearLayers()
  }

  if (localCoordinates.value) {
    const isActive = status.value.active
    const strokeColor = isActive ? 'var(--ace-primary)' : 'var(--ace-secondary)'
    const fillColor = isActive ? 'var(--ace-primary)' : 'var(--ace-secondary)'
    
    if (localType.value === 'circle') {
      const center = localCoordinates.value.center
      const radius = localCoordinates.value.radius
      renderedActiveShape = L.circle(center, {
        radius: radius,
        color: strokeColor,
        weight: 3,
        fillColor: fillColor,
        fillOpacity: 0.15
      }).addTo(drawLayer)
      
      staticFitBoundOnce(renderedActiveShape.getBounds())
      
    } else if (localType.value === 'polygon') {
      const poly = localCoordinates.value.polygon
      renderedActiveShape = L.polygon(poly, {
        color: strokeColor,
        weight: 3,
        fillColor: fillColor,
        fillOpacity: 0.15
      }).addTo(drawLayer)
      
      staticFitBoundOnce(renderedActiveShape.getBounds())
    }
  }

  // 2. Render recent strikes
  // Remove markers for strikes no longer in list
  const currentStrikeIds = new Set(status.value.recent_strikes.map(s => s.id))
  for (const id in strikeMarkers) {
    if (!currentStrikeIds.has(id)) {
      strikeGroup.removeLayer(strikeMarkers[id])
      delete strikeMarkers[id]
    }
  }

  // Add markers for new strikes
  status.value.recent_strikes.forEach(strike => {
    if (!strikeMarkers[strike.id]) {
      const marker = L.circleMarker([strike.lat, strike.lon], {
        radius: 8,
        color: 'var(--ace-danger)',
        fillColor: 'var(--ace-danger)',
        fillOpacity: 0.85,
        weight: 2
      }).addTo(strikeGroup)

      const dateStr = new Date(strike.dateTimeISO).toLocaleString()
      const distStr = strike.distance_meters !== null 
        ? `<br>Distance: <strong>${(strike.distance_meters/1000).toFixed(2)} km</strong>`
        : ''
      const intensityStr = strike.intensity !== null 
        ? `<br>Intensity: <strong>${strike.intensity} kA</strong>` 
        : ''
      
      marker.bindPopup(`
        <div style="font-family: var(--ace-font-body); padding: 5px;">
          <h4 style="margin: 0 0 5px; color: var(--ace-danger); font-family: var(--ace-font-heading);">⚡ Strike Detected</h4>
          Time: <strong>${dateStr}</strong>
          ${distStr}
          ${intensityStr}
          <br>Type: <strong>${strike.type}</strong>
          <br><br>
          <a href="https://www.google.com/maps/search/?api=1&query=${strike.lat},${strike.lon}" target="_blank" style="color: var(--ace-secondary); text-decoration: underline; font-weight: 700;">View on Google Maps</a>
        </div>
      `)

      strikeMarkers[strike.id] = marker
    }
  })
}

const staticFitBoundOnce = (bounds) => {
  if (!hasFittedBounds && bounds) {
    map.fitBounds(bounds, { padding: [50, 50] })
    hasFittedBounds = true
  }
}

watch(() => status.value.active, (newVal) => {
  if (!newVal) {
    hasFittedBounds = false
  }
})

const panToStrike = (strike) => {
  if (map && strike) {
    map.setView([strike.lat, strike.lon], 11)
    const marker = strikeMarkers[strike.id]
    if (marker) {
      marker.openPopup()
    }
  }
}

const formatTime = (isoString) => {
  return new Date(isoString).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

// API Calls
const fetchConfig = async () => {
  try {
    const resp = await fetch(`${API_BASE}/api/config`)
    if (resp.ok) {
      configStatus.value = await resp.json()
    }
  } catch (err) {
    console.error('Failed to fetch config status:', err)
  }
}

const saveConfig = async () => {
  savingConfig.value = true
  try {
    const resp = await fetch(`${API_BASE}/api/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        xweather_api_key: configForm.value.xweather_api_key,
        discord_webhook: configForm.value.discord_webhook
      })
    })
    
    if (resp.ok) {
      alert('Configuration saved successfully!')
      configForm.value.xweather_api_key = ''
      configForm.value.discord_webhook = ''
      await fetchConfig()
      await fetchStatus()
    } else {
      const errData = await resp.json()
      alert(`Failed to save config: ${errData.detail || 'Unknown error'}`)
    }
  } catch (err) {
    alert(`Error saving configuration: ${err.message}`)
  } finally {
    savingConfig.value = false
  }
}

const testDiscordWebhook = async () => {
  testingWebhook.value = true
  try {
    const resp = await fetch(`${API_BASE}/api/test-discord`, { method: 'POST' })
    const data = await resp.json()
    if (resp.ok) {
      alert('Test alert sent successfully to Discord!')
    } else {
      alert(`Webhook Test Failed: ${data.detail || 'Unknown error'}`)
    }
  } catch (err) {
    alert(`Error: ${err.message}`)
  } finally {
    testingWebhook.value = false
  }
}

const startMonitoring = async () => {
  if (!localCoordinates.value) return
  
  try {
    const payload = {
      type: localType.value,
      coordinates: localCoordinates.value,
      timeout_hours: timeoutHours.value
    }
    
    const resp = await fetch(`${API_BASE}/api/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
    
    if (resp.ok) {
      await fetchStatus()
    } else {
      const errData = await resp.json()
      alert(`Failed to start: ${errData.detail || 'Unknown error'}`)
    }
  } catch (err) {
    alert(`Network error starting monitoring: ${err.message}`)
  }
}

const stopMonitoring = async () => {
  try {
    const resp = await fetch(`${API_BASE}/api/stop`, { method: 'POST' })
    if (resp.ok) {
      await fetchStatus()
    }
  } catch (err) {
    alert(`Error stopping: ${err.message}`)
  }
}
</script>

<template>
  <div class="sentinel-app">
    <!-- Sidebar / Control Panel -->
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-badge">⚡ FLASH</span>
        <h2>Flash Finder</h2>
        <p class="brand-sub">Real-time lightning threat monitoring</p>
      </div>

      <div class="control-groups">
        <!-- Configuration Card -->
        <div class="card control-card">
          <h3 class="card-title">1. Define Detection Area</h3>
          
          <div class="draw-actions">
            <button 
              class="ace-button ace-button--sm"
              :class="drawMode === 'circle' ? 'ace-button--secondary' : 'ace-button--ghost'"
              :disabled="status.active"
              @click="startDrawingCircle"
            >
              ⭕ Circle
            </button>
            <button 
              class="ace-button ace-button--sm"
              :class="drawMode === 'polygon' ? 'ace-button--secondary' : 'ace-button--ghost'"
              :disabled="status.active"
              @click="startDrawingPolygon"
            >
              ⬡ Polygon
            </button>
            <button 
              class="ace-button ace-button--sm ace-button--danger"
              v-if="hasDrawnShape"
              :disabled="status.active"
              @click="clearDrawnShape"
            >
              🗑️ Clear
            </button>
          </div>
          
          <div v-if="drawMode === 'polygon' && tempPoints.length >= 3" class="complete-btn-container">
            <button 
              class="ace-button ace-button--sm ace-button--primary ace-button--block"
              @click="completePolygon"
            >
              ✓ Complete Polygon
            </button>
          </div>
          
          <div v-if="drawHelpText" class="help-text">
            {{ drawHelpText }}
          </div>

          <div v-if="hasDrawnShape" class="shape-info">
            <span class="ace-badge ace-badge--info">Area Defined</span>
            <span class="shape-details">{{ shapeDetailsText }}</span>
          </div>
        </div>

        <!-- Monitoring Settings -->
        <div class="card control-card">
          <h3 class="card-title">2. Alert Configuration</h3>
          
          <div class="ace-field">
            <label class="ace-field__label">Timeout Duration</label>
            <div class="slider-container">
              <input 
                type="range" 
                min="1" 
                max="4" 
                step="0.5" 
                v-model.number="timeoutHours"
                :disabled="status.active"
                class="ace-input-slider"
              />
              <span class="slider-value">{{ timeoutHours }} hrs</span>
            </div>
            <div class="ace-field__hint">Monitoring will automatically stop after this duration.</div>
          </div>
        </div>

        <!-- Credentials Config Card -->
        <div class="card control-card">
          <h3 class="card-title">⚙️ Credentials Settings</h3>
          
          <div class="ace-field">
            <label class="ace-field__label">XWeather API Key</label>
            <input 
              type="password" 
              v-model="configForm.xweather_api_key"
              :placeholder="configStatus.xweather_configured ? configStatus.xweather_api_key_masked : 'Enter client_id_client_secret'"
              class="ace-input"
            />
          </div>

          <div class="ace-field">
            <label class="ace-field__label">Discord Webhook</label>
            <input 
              type="password" 
              v-model="configForm.discord_webhook"
              :placeholder="configStatus.discord_configured ? configStatus.discord_webhook_masked : 'Enter Discord Webhook URL'"
              class="ace-input"
            />
          </div>

          <div class="config-actions">
            <button 
              class="ace-button ace-button--sm ace-button--primary ace-button--block"
              @click="saveConfig"
              :disabled="savingConfig"
            >
              {{ savingConfig ? 'Saving...' : 'Save Configuration' }}
            </button>
          </div>
          
          <div class="test-webhook-container" style="margin-top: 0.5rem;" v-if="configStatus.discord_configured">
            <button 
              class="ace-button ace-button--sm ace-button--ghost ace-button--block"
              @click="testDiscordWebhook"
              :disabled="testingWebhook"
            >
              {{ testingWebhook ? 'Sending Test...' : 'Test Webhook' }}
            </button>
          </div>
        </div>

        <!-- Action / Status Card -->
        <div class="card status-card">
          <div class="status-header">
            <h3 class="card-title">Flash Finder Status</h3>
            <span 
              class="ace-badge" 
              :class="status.active ? 'ace-badge--success' : 'ace-badge--neutral'"
            >
              {{ status.active ? 'Active' : 'Inactive' }}
            </span>
          </div>

          <div class="status-stats" v-if="status.active">
            <div class="stat-item">
              <span class="stat-label">Checks</span>
              <span class="stat-value">{{ status.checks_count }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">Strikes</span>
              <span class="stat-value danger-text">{{ status.detections_count }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">Remaining</span>
              <span class="stat-value">{{ timeRemainingText }}</span>
            </div>
          </div>

          <div class="start-stop-actions">
            <div v-if="!isConfigured" class="config-warning">
              ⚠️ Please configure XWeather and Discord keys below to start monitoring.
            </div>
            <button 
              v-if="!status.active"
              class="ace-button ace-button--md ace-button--primary ace-button--block"
              :disabled="!hasDrawnShape || !isConfigured"
              @click="startMonitoring"
            >
              ▶ Start Monitoring
            </button>
            <button 
              v-else
              class="ace-button ace-button--md ace-button--danger ace-button--block"
              @click="stopMonitoring"
            >
              ⏹ Stop Monitoring
            </button>
          </div>
        </div>
      </div>
    </aside>

    <!-- Main Content Area -->
    <main class="content">
      <!-- Leaflet Map -->
      <div class="map-container">
        <div id="map"></div>
      </div>

      <!-- Logs and Detections Tabs -->
      <div class="details-panel">
        <div class="details-tabs">
          <button 
            class="tab-btn" 
            :class="{ active: activeTab === 'strikes' }"
            @click="activeTab = 'strikes'"
          >
            ⚡ Strikes ({{ status.recent_strikes.length }})
          </button>
          <button 
            class="tab-btn" 
            :class="{ active: activeTab === 'logs' }"
            @click="activeTab = 'logs'"
          >
            📋 Logs ({{ status.logs.length }})
          </button>
        </div>

        <div class="tab-content card">
          <!-- Strikes List -->
          <div v-if="activeTab === 'strikes'" class="strikes-tab">
            <div v-if="status.recent_strikes.length === 0" class="empty-state">
              No lightning strikes detected in this session.
            </div>
            <div v-else class="list-wrapper">
              <div 
                v-for="strike in sortedStrikes" 
                :key="strike.id" 
                class="strike-item"
                @click="panToStrike(strike)"
              >
                <div class="strike-left">
                  <div class="strike-meta">
                    <span class="strike-time">{{ formatTime(strike.dateTimeISO) }}</span>
                    <span class="strike-type ace-badge ace-badge--neutral">Type: {{ strike.type }}</span>
                    <span class="strike-intensity ace-badge ace-badge--danger" v-if="strike.intensity">
                      {{ strike.intensity }} kA
                    </span>
                  </div>
                  <div class="strike-loc">
                    <span>📍 Lat: {{ strike.lat.toFixed(4) }}, Lon: {{ strike.lon.toFixed(4) }}</span>
                    <span v-if="strike.distance_meters !== null" class="strike-dist">
                      ({{ (strike.distance_meters / 1000).toFixed(2) }} km away)
                    </span>
                  </div>
                </div>
                <a 
                  :href="`https://www.google.com/maps/search/?api=1&query=${strike.lat},${strike.lon}`" 
                  target="_blank" 
                  class="map-link ace-button ace-button--quiet ace-button--link"
                  @click.stop
                >
                  Open Maps ↗
                </a>
              </div>
            </div>
          </div>

          <!-- Logs List -->
          <div v-if="activeTab === 'logs'" class="logs-tab">
            <div v-if="status.logs.length === 0" class="empty-state">
              No logs available.
            </div>
            <div v-else class="list-wrapper logs-list">
              <div v-for="(log, idx) in status.logs.slice().reverse()" :key="idx" class="log-item">
                {{ log }}
              </div>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<style scoped>
.sentinel-app {
  display: grid;
  grid-template-columns: 380px 1fr;
  height: 100vh;
  background: var(--ace-bg);
  color: var(--ace-text);
  overflow: hidden;
}

.sidebar {
  background: var(--ace-bg-panel);
  border-right: 1px solid var(--ace-border-strong);
  padding: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
  overflow-y: auto;
}

.brand {
  border-bottom: 1px solid var(--ace-border);
  padding-bottom: 1rem;
}

.brand-badge {
  background: var(--ace-primary-soft);
  color: var(--ace-primary);
  font-weight: 800;
  font-size: 0.7rem;
  padding: 0.2rem 0.5rem;
  border-radius: var(--ace-radius-sm);
  border: 1px solid var(--ace-primary);
  display: inline-block;
  margin-bottom: 0.5rem;
}

.brand h2 {
  font-family: var(--ace-font-heading);
  margin: 0;
  font-size: 1.5rem;
}

.brand-sub {
  color: var(--ace-text-subtle);
  font-size: 0.8rem;
  margin: 0.25rem 0 0;
}

.control-groups {
  display: flex;
  flex-direction: column;
  gap: 1.2rem;
}

.control-card {
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.8rem;
}

.card-title {
  margin: 0;
  font-size: 0.95rem;
  font-weight: 700;
  color: var(--ace-text);
}

.draw-actions {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.draw-actions button {
  flex: 1 0 auto;
}

.complete-btn-container {
  margin-top: 0.2rem;
}

.help-text {
  font-size: 0.76rem;
  color: var(--ace-secondary);
  background: var(--ace-secondary-soft);
  padding: 0.5rem;
  border-radius: var(--ace-radius-sm);
  border: 1px solid var(--ace-focus);
}

.shape-info {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.78rem;
  background: rgba(255, 255, 255, 0.03);
  padding: 0.5rem;
  border-radius: var(--ace-radius-sm);
  border: 1px solid var(--ace-border);
}

.shape-details {
  color: var(--ace-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex-grow: 1;
}

.slider-container {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  margin-top: 0.3rem;
}

.ace-input-slider {
  flex-grow: 1;
  accent-color: var(--ace-primary);
}

.slider-value {
  font-weight: 700;
  color: var(--ace-primary);
  font-size: 0.9rem;
  min-width: 3.5rem;
  text-align: right;
}

.status-card {
  padding: 1.2rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.status-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.status-stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 0.5rem;
  background: rgba(0, 0, 0, 0.2);
  padding: 0.8rem;
  border-radius: var(--ace-radius-md);
  border: 1px solid var(--ace-border);
}

.stat-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
}

.stat-label {
  font-size: 0.68rem;
  color: var(--ace-text-subtle);
  text-transform: uppercase;
  font-weight: 700;
}

.stat-value {
  font-size: 1rem;
  font-weight: 800;
  color: var(--ace-text);
}

.danger-text {
  color: var(--ace-danger);
}

.content {
  display: grid;
  grid-template-rows: 1fr 280px;
  height: 100vh;
  overflow: hidden;
}

.map-container {
  position: relative;
  width: 100%;
  height: 100%;
}

#map {
  width: 100%;
  height: 100%;
  border: none;
  border-radius: 0;
}

.details-panel {
  background: var(--ace-bg-elevated);
  border-top: 1px solid var(--ace-border-strong);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.details-tabs {
  display: flex;
  background: var(--ace-bg-panel);
  border-bottom: 1px solid var(--ace-border);
}

.tab-btn {
  background: none;
  border: none;
  color: var(--ace-text-muted);
  padding: 0.8rem 1.5rem;
  font-weight: 700;
  font-size: 0.85rem;
  cursor: pointer;
  border-right: 1px solid var(--ace-border);
  transition: all 0.15s;
}

.tab-btn:hover {
  color: var(--ace-text);
  background: rgba(255, 255, 255, 0.02);
}

.tab-btn.active {
  color: var(--ace-primary);
  background: var(--ace-bg-elevated);
  border-bottom: 2px solid var(--ace-primary);
}

.tab-content {
  flex-grow: 1;
  overflow-y: auto;
  border-radius: 0;
  border: none;
  padding: 1rem;
}

.empty-state {
  display: flex;
  justify-content: center;
  align-items: center;
  height: 100%;
  color: var(--ace-text-subtle);
  font-size: 0.88rem;
}

.list-wrapper {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.strike-item {
  background: var(--ace-bg-panel);
  border: 1px solid var(--ace-border);
  border-radius: var(--ace-radius-md);
  padding: 0.8rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
  cursor: pointer;
  transition: all 0.15s;
}

.strike-item:hover {
  border-color: var(--ace-secondary-soft);
  background: rgba(255, 255, 255, 0.02);
  transform: translateY(-1px);
}

.strike-left {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.strike-meta {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}

.strike-time {
  font-weight: 700;
  font-size: 0.84rem;
}

.strike-loc {
  font-size: 0.78rem;
  color: var(--ace-text-muted);
  display: flex;
  gap: 0.8rem;
}

.strike-dist {
  color: var(--ace-secondary);
  font-weight: 600;
}

.map-link {
  font-size: 0.8rem;
  align-self: center;
}

.logs-list {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 0.78rem;
  color: var(--ace-text-muted);
  gap: 0.3rem;
}

.log-item {
  padding: 0.2rem 0.4rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.02);
}

.config-warning {
  font-size: 0.76rem;
  color: var(--ace-danger);
  background: var(--ace-danger-soft);
  padding: 0.5rem;
  border-radius: var(--ace-radius-sm);
  border: 1px solid rgba(246, 81, 29, 0.3);
  margin-bottom: 0.8rem;
  text-align: center;
}
</style>
