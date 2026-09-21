import { BrowserRouter, Navigate, Routes, Route } from 'react-router-dom'
import { useEffect } from 'react'
import { SpeedInsights } from '@vercel/speed-insights/react'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Analysis from './pages/Analysis'
import Reports from './pages/Reports'
import Settings from './pages/Settings'
import Portfolio from './pages/Portfolio'
import TrackingBoard from './pages/TrackingBoard'
import Recommendations from './pages/Recommendations'
import RecommendationInsights from './pages/RecommendationInsights'
import QualityInsights from './pages/QualityInsights'
import PaperTrading from './pages/PaperTrading'
import Login from './pages/Login'
import Feedback from './pages/Feedback'
import ModelProfiles from './pages/ModelProfiles'
import Mainline from './pages/Mainline'
import { useAuthStore } from './stores/authStore'

function RequireAuth({ children }: { children: JSX.Element }) {
  const { user, hydrated, hydrate } = useAuthStore()

  useEffect(() => {
    if (!hydrated) void hydrate()
  }, [hydrated, hydrate])

  if (!hydrated) {
    return <div className="min-h-screen flex items-center justify-center text-slate-500">加载中...</div>
  }

  if (!user) {
    return <Navigate to="/login" replace />
  }

  return children
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="*"
          element={
            <RequireAuth>
              <Layout>
                <Routes>
                  <Route path="/" element={<Dashboard />} />
                  <Route path="/tracking-board" element={<TrackingBoard />} />
                  <Route path="/analysis" element={<Analysis />} />
                  <Route path="/mainline" element={<Mainline />} />
                  <Route path="/model-profiles" element={<ModelProfiles />} />
                  <Route path="/reports" element={<Reports />} />
                  <Route path="/portfolio" element={<Portfolio />} />
                  <Route path="/recommendations" element={<Recommendations />} />
                  <Route path="/recommendation-insights" element={<RecommendationInsights />} />
                  <Route path="/quality-insights" element={<QualityInsights />} />
                  <Route path="/paper-trading" element={<PaperTrading />} />
                  <Route path="/settings" element={<Settings />} />
                  <Route path="/feedback" element={<Feedback />} />
                </Routes>
              </Layout>
            </RequireAuth>
          }
        />
      </Routes>
      <SpeedInsights />
    </BrowserRouter>
  )
}

export default App
