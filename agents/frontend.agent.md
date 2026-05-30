# Frontend Agent — Mockingbird

> **Scope:** Next.js 15 web application, React components, pages, routing, styling, state management.

---

## Identity

You are the Frontend Agent for Mockingbird, a real-time voice cloning web application. Your responsibility is building a stunning, premium web interface using Next.js 15 (App Router) with React 19 and TypeScript. The UI should feel modern, polished, and professional — think dark mode, glassmorphism, smooth animations, and premium typography.

---

## Tech Stack

| Technology | Purpose |
|-----------|---------|
| **Next.js 15** | App Router, SSR, API routes |
| **React 19** | UI components |
| **TypeScript** | Type safety |
| **Zustand** | State management |
| **D3.js / Canvas API** | Audio visualizations (waveforms, spectrograms) |
| **Framer Motion** | Animations and transitions |
| **CSS Modules** | Component-scoped styling |
| **Twilio Client SDK** | WebRTC calling integration |

---

## Design System

### Color Palette (Dark Mode Primary)

```css
:root {
  /* Backgrounds */
  --bg-primary: #0a0a0f;
  --bg-secondary: #12121a;
  --bg-tertiary: #1a1a2e;
  --bg-glass: rgba(255, 255, 255, 0.03);

  /* Accents */
  --accent-primary: #6c5ce7;    /* Purple */
  --accent-secondary: #00cec9;   /* Teal */
  --accent-gradient: linear-gradient(135deg, #6c5ce7, #00cec9);
  --accent-glow: 0 0 20px rgba(108, 92, 231, 0.3);

  /* Text */
  --text-primary: #f0f0f5;
  --text-secondary: #8888a0;
  --text-muted: #555566;

  /* Status */
  --status-success: #00b894;
  --status-warning: #fdcb6e;
  --status-error: #ff6b6b;
  --status-active: #6c5ce7;

  /* Borders */
  --border-subtle: rgba(255, 255, 255, 0.06);
  --border-accent: rgba(108, 92, 231, 0.3);
}
```

### Typography
- **Primary Font**: `Inter` (Google Fonts) — UI text, labels, body
- **Monospace**: `JetBrains Mono` — Code, metrics, latency numbers
- **Display**: `Outfit` — Headlines, large text

### Component Patterns
- Glassmorphism cards with `backdrop-filter: blur(20px)`
- Subtle border glow on interactive elements
- Micro-animations on hover (scale, glow, color shift)
- Smooth page transitions with Framer Motion
- Responsive grid layouts (mobile-first)

---

## Pages to Implement

### 1. Dashboard (`/`)
- Welcome message with user's active voice model
- Quick-start actions: "Make a Call", "Train New Voice", "Live Preview"
- Recent calls list with latency metrics
- Usage stats (minutes used / minutes remaining)
- Active voice model card with preview button

### 2. Voice Studio (`/studio`)
- **Create Voice** wizard:
  1. Choose mode (Instant Clone / HD Clone)
  2. Upload audio or record in-browser
  3. Audio quality validation (noise level, duration, clipping)
  4. Training progress (HD Clone) or instant result
  5. Preview and adjust settings
- **Voice Library** — Grid of voice model cards with:
  - Name, quality tier badge (Instant/HD)
  - Similarity score
  - Preview play button
  - Edit / Delete actions
  - "Set Active" button
- **Voice Editor** — Adjust pitch, speed, breathiness with real-time preview

### 3. Dialer (`/dialer`)
- Full phone dialer UI:
  - Numeric keypad (0-9, *, #)
  - Phone number input with country code selector
  - Contacts list (recent + saved)
  - "Call" button with voice model indicator
- Active call screen:
  - Timer, voice model name
  - Real-time waveform (input vs output)
  - Latency indicator (color-coded)
  - Controls: mute, hold, voice on/off toggle, model switcher, end call
  - Volume slider
- Call history with duration, model used, avg latency

### 4. Live Monitor (`/monitor`)
- Split-screen: input waveform (top) vs output waveform (bottom)
- Real-time spectrogram visualization
- Latency chart (rolling 60-second window)
- Voice similarity meter
- Audio level meters (input/output)
- "Start Preview" button (mic → transform → speaker)

### 5. Settings (`/settings`)
- **Audio**: Input/output device selection, sample rate, buffer size
- **Quality**: Noise suppression, echo cancellation, auto gain
- **Account**: Email, plan, billing
- **Phone**: Twilio phone number management
- **Advanced**: WebSocket URL override, debug logging

---

## Component Library

### Core Components
- `<VoiceCard />` — Displays a voice model with preview, quality badge, actions
- `<AudioVisualizer />` — Real-time waveform/spectrogram canvas rendering
- `<LatencyIndicator />` — Color-coded latency display (green < 200ms, yellow < 300ms, red > 300ms)
- `<DialPad />` — Phone dialer keypad with DTMF tones
- `<ActiveCallBar />` — Persistent bar during active call (minimizes to bottom)
- `<VoiceModelSwitcher />` — Dropdown to switch active voice model
- `<AudioRecorder />` — Record audio samples in-browser with quality meter
- `<UploadZone />` — Drag-and-drop audio file upload with validation
- `<TrainingProgress />` — Animated progress bar for HD Clone training
- `<WaveformCompare />` — Side-by-side original vs transformed waveform

### Layout Components
- `<AppShell />` — Sidebar navigation + top bar + main content area
- `<Sidebar />` — Navigation with icons, active state, collapse toggle
- `<TopBar />` — Active voice model display, user avatar, settings
- `<GlassCard />` — Reusable glassmorphism container

---

## State Management (Zustand Stores)

```typescript
// stores/audioStore.ts
interface AudioStore {
  isListening: boolean;
  isTransforming: boolean;
  inputLevel: number;       // 0-1 RMS level
  outputLevel: number;
  currentLatency: number;   // ms
  connectionStatus: 'disconnected' | 'connecting' | 'connected';
  
  startListening: () => void;
  stopListening: () => void;
  toggleTransform: () => void;
}

// stores/voiceStore.ts
interface VoiceStore {
  activeModelId: string | null;
  models: VoiceModel[];
  trainingJobs: TrainingJob[];
  
  setActiveModel: (id: string) => void;
  fetchModels: () => Promise<void>;
  createModel: (data: CreateModelInput) => Promise<VoiceModel>;
  deleteModel: (id: string) => Promise<void>;
}

// stores/callStore.ts
interface CallStore {
  activeCall: ActiveCall | null;
  callHistory: CallRecord[];
  
  initiateCall: (phoneNumber: string) => Promise<void>;
  endCall: () => void;
  toggleMute: () => void;
  toggleHold: () => void;
}
```

---

## Integration Points

### Audio Engine
The frontend integrates with the Audio Engine (see `audio-engine.agent.md`) via:
- `AudioEngine` class instance (created in a React context provider)
- Event listeners for state changes (connection, latency, levels)
- Methods: `start()`, `stop()`, `setModel(id)`, `getMetrics()`

### Gateway API
- REST API calls via `fetch` or a lightweight client (no Axios — use native fetch)
- Base URL from environment variable: `NEXT_PUBLIC_API_URL`
- JWT token in `Authorization: Bearer <token>` header

### Twilio
- `@twilio/voice-sdk` for WebRTC calling
- Device initialization in a React context
- Call events mapped to Zustand store updates

---

## Implementation Order

1. **Project setup**: Initialize Next.js 15 with TypeScript, install dependencies
2. **Design system**: Create CSS variables, global styles, font loading
3. **Layout**: `AppShell`, `Sidebar`, `TopBar` components
4. **Dashboard page**: Static layout, then wire up to stores
5. **Voice Studio**: Upload, training wizard, voice library grid
6. **Audio Engine integration**: Connect to AudioEngine class for real-time features
7. **Live Monitor**: Waveform visualizer, latency chart
8. **Dialer**: Dial pad, call screen, Twilio integration
9. **Settings**: Audio configuration, account management
10. **Polish**: Animations, responsive design, error states, loading states

---

## Constraints

- All pages must work without JavaScript for initial render (SSR)
- No external CSS frameworks (no Tailwind unless user requests)
- Audio processing code must NOT live in React components — it belongs in the Audio Engine
- Use CSS Modules for component styling, CSS variables for theming
- All interactive elements must have unique IDs for testing
- Images must be optimized (`next/image`)
- Accessibility: ARIA labels on all interactive elements, keyboard navigation support
