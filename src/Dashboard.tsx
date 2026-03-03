import React, { useState, useEffect, useRef } from 'react';
import { 
  Search, Play, ShieldAlert, MessageSquare, LayoutGrid, Terminal, Activity, 
  ChevronRight, Cpu, BarChart3, Globe, LineChart,
  Zap, AlertTriangle, CheckCircle2, Sun, Moon, Send, Bot
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { createChart, IChartApi, ISeriesApi, CandlestickData, CandlestickSeries, ColorType, BusinessDay } from 'lightweight-charts';
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

type AgentStatus = 'pending' | 'in_progress' | 'completed' | 'skipped' | 'failed';
interface Agent { name: string; displayName: string; team: string; status: AgentStatus; }
interface Message { 
  role: 'user' | 'assistant';
  agent?: string; 
  content: string; 
  timestamp: string; 
  type?: 'text' | 'report'; 
}

const INITIAL_AGENTS: Agent[] = [
  { name: 'Market Analyst', displayName: '市场分析师', team: 'Analyst', status: 'pending' },
  { name: 'Social Analyst', displayName: '社交舆情分析师', team: 'Analyst', status: 'pending' },
  { name: 'News Analyst', displayName: '新闻分析师', team: 'Analyst', status: 'pending' },
  { name: 'Fundamentals Analyst', displayName: '基本面分析师', team: 'Analyst', status: 'pending' },
  { name: 'Bull Researcher', displayName: '多头研究员', team: 'Research', status: 'pending' },
  { name: 'Bear Researcher', displayName: '空头研究员', team: 'Research', status: 'pending' },
  { name: 'Research Manager', displayName: '研究经理', team: 'Research', status: 'pending' },
  { name: 'Trader', displayName: '交易员', team: 'Trading', status: 'pending' },
  { name: 'Aggressive Analyst', displayName: '激进型风险分析师', team: 'Risk', status: 'pending' },
  { name: 'Neutral Analyst', displayName: '中性风险分析师', team: 'Risk', status: 'pending' },
  { name: 'Conservative Analyst', displayName: '保守型风险分析师', team: 'Risk', status: 'pending' },
  { name: 'Portfolio Manager', displayName: '投资组合经理', team: 'Portfolio', status: 'pending' },
];

const AGENT_I18N: Record<string, string> = {
  'Market Analyst': '市场分析师',
  'Social Analyst': '社交舆情分析师',
  'News Analyst': '新闻分析师',
  'Fundamentals Analyst': '基本面分析师',
  'Bull Researcher': '多头研究员',
  'Bear Researcher': '空头研究员',
  'Research Manager': '研究经理',
  'Trader': '交易员',
  'Aggressive Analyst': '激进型风险分析师',
  'Neutral Analyst': '中性风险分析师',
  'Conservative Analyst': '保守型风险分析师',
  'Portfolio Manager': '投资组合经理',
};

const localizeAgent = (name?: string) => {
  if (!name) return '';
  if (name.startsWith('REPORT: ')) return `报告：${name.slice(8)}`;
  if (name === 'SYSTEM') return '系统';
  return AGENT_I18N[name] || name;
};

export default function Dashboard() {
  const detectDarkMode = (): boolean => {
    if (typeof window === 'undefined') return false;
    const bySystem = window.matchMedia?.('(prefers-color-scheme: dark)')?.matches ?? false;
    const hour = new Date().getHours();
    const byTime = hour >= 19 || hour < 7;
    return bySystem || byTime;
  };

  const [input, setInput] = useState("");
  const [agents, setAgents] = useState<Agent[]>(INITIAL_AGENTS);
  const [messages, setMessages] = useState<Message[]>([]);
  const [decision, setDecision] = useState<{ action: string; confidence: string } | null>(null);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [isDarkMode, setIsDarkMode] = useState<boolean>(() => detectDarkMode());
  const [themeMode, setThemeMode] = useState<'auto' | 'manual'>('auto');
  const [currentSymbol, setCurrentSymbol] = useState<string | null>(null);
  
  const scrollRef = useRef<HTMLDivElement>(null);
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<{ chart: IChartApi; series: ISeriesApi<"Candlestick"> } | null>(null);

  const formatLocalDate = (d: Date): string => {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  };

  const toBusinessDay = (s: string): BusinessDay | null => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
    if (!m) return null;
    const y = Number(m[1]);
    const mo = Number(m[2]);
    const d = Number(m[3]);
    if (!Number.isFinite(y) || !Number.isFinite(mo) || !Number.isFinite(d)) return null;
    return { year: y, month: mo, day: d };
  };

  useEffect(() => {
    document.documentElement.classList.toggle('dark', isDarkMode);
  }, [isDarkMode]);

  useEffect(() => {
    if (themeMode !== 'auto' || typeof window === 'undefined') return;
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const refresh = () => setIsDarkMode(detectDarkMode());
    refresh();
    media.addEventListener?.('change', refresh);
    const timer = window.setInterval(refresh, 60_000);
    return () => {
      media.removeEventListener?.('change', refresh);
      window.clearInterval(timer);
    };
  }, [themeMode]);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages]);

  // K-Line Chart Initialization
  useEffect(() => {
    if (!chartContainerRef.current) return;
    let chart: IChartApi | null = null;
    try {
      chart = createChart(chartContainerRef.current, {
        layout: { 
          background: { type: ColorType.Solid, color: 'transparent' },
          textColor: isDarkMode ? '#6B7280' : '#374151',
        },
        grid: {
          vertLines: { color: isDarkMode ? 'rgba(31, 41, 55, 0.1)' : 'rgba(229, 231, 235, 0.5)' },
          horzLines: { color: isDarkMode ? 'rgba(31, 41, 55, 0.1)' : 'rgba(229, 231, 235, 0.5)' },
        },
        width: chartContainerRef.current.clientWidth,
        height: chartContainerRef.current.clientHeight,
      });
      const series = chart.addSeries(CandlestickSeries, {
        upColor: '#ef4444', downColor: '#10b981', borderVisible: false,
        wickUpColor: '#ef4444', wickDownColor: '#10b981',
      });
      chartRef.current = { chart, series };
      if (currentSymbol) fetchKlines(currentSymbol);
      const handleResize = () => {
        if (chartContainerRef.current && chart) chart.applyOptions({ width: chartContainerRef.current.clientWidth, height: chartContainerRef.current.clientHeight });
      };
      window.addEventListener('resize', handleResize);
      return () => {
        window.removeEventListener('resize', handleResize);
        if (chart) { chart.remove(); chartRef.current = null; }
      };
    } catch (error) { console.error("Chart failed", error); }
  }, [isDarkMode]);

  const fetchKlines = async (symbol: string) => {
    try {
      const end = new Date();
      const start = new Date(end.getTime() - 180 * 24 * 60 * 60 * 1000);
      const query = new URLSearchParams({
        symbol,
        start_date: formatLocalDate(start),
        end_date: formatLocalDate(end),
      });
      const url = `http://localhost:8000/v1/market/kline?${query.toString()}`;
      console.log('[kline] request=%s', url);
      const resp = await fetch(url);
      if (!resp.ok) return;
      const data = await resp.json();
      if (data.candles && chartRef.current) {
        const formattedData: CandlestickData[] = data.candles
          .map((c: any) => ({
            time: toBusinessDay(String(c.date || '').slice(0, 10)),
            open: Number(c.open),
            high: Number(c.high),
            low: Number(c.low),
            close: Number(c.close),
          }))
          .filter((c: any) =>
            c.time &&
            Number.isFinite(c.open) &&
            Number.isFinite(c.high) &&
            Number.isFinite(c.low) &&
            Number.isFinite(c.close)
          )
          .sort((a, b) => {
            const aa = a.time as BusinessDay;
            const bb = b.time as BusinessDay;
            const ka = aa.year * 10000 + aa.month * 100 + aa.day;
            const kb = bb.year * 10000 + bb.month * 100 + bb.day;
            return ka - kb;
          })
          .filter((item, idx, arr) => {
            if (idx === 0) return true;
            const a = item.time as BusinessDay;
            const b = arr[idx - 1].time as BusinessDay;
            return !(a.year === b.year && a.month === b.month && a.day === b.day);
          });
        console.log('[kline] symbol=%s bars=%d', symbol, formattedData.length);
        chartRef.current.series.setData(formattedData);
        chartRef.current.chart.timeScale().fitContent();
      }
    } catch (e) { console.error("Kline error", e); }
  };

  const startAnalysis = async () => {
    if (!input || isAnalyzing) return;
    
    const userText = input;
    setInput("");
    setIsAnalyzing(true);
    setAgents(INITIAL_AGENTS.map(a => ({ ...a, status: 'pending' })));
    setMessages(prev => [...prev, { role: 'user', content: userText, timestamp: new Date().toLocaleTimeString() }]);
    setDecision(null);

    try {
      const response = await fetch('http://localhost:8000/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: [{ role: 'user', content: userText }], stream: true }),
      });

      if (!response.ok) throw new Error("Connection failed");
      const reader = response.body?.getReader();
      const decoder = new TextDecoder();
      if (!reader) return;

      let currentEvent = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value);
        chunk.split('\n').forEach(line => {
          const trimmed = line.trim();
          if (!trimmed) return;
          if (trimmed.startsWith('event: ')) currentEvent = trimmed.replace('event: ', '').trim();
          else if (trimmed.startsWith('data: ')) {
            const dataStr = trimmed.replace('data: ', '').trim();
            if (dataStr === '[DONE]') return;
            try {
              const data = JSON.parse(dataStr);
              if (['job.created', 'job.running', 'job.ready'].includes(currentEvent)) {
                if (data.symbol && data.symbol !== currentSymbol) {
                  setCurrentSymbol(data.symbol);
                  fetchKlines(data.symbol);
                }
              }
              handleSSEEvent({ event: currentEvent, data });
              currentEvent = '';
            } catch (e) {}
          }
        });
      }
    } catch (error) { setIsAnalyzing(false); }
  };

  const handleSSEEvent = (payload: any) => {
    const { event, data } = payload;
    switch (event) {
      case 'agent.status':
        setAgents(prev => prev.map(a => a.name === data.agent ? { ...a, status: data.status } : a));
        break;
      case 'agent.message':
        setMessages(prev => [...prev, { 
          role: 'assistant', agent: data.agent || 'SYSTEM', 
          content: data.content, timestamp: new Date().toLocaleTimeString(), type: 'text' 
        }]);
        break;
      case 'agent.report':
        setMessages(prev => [...prev, { 
          role: 'assistant', agent: `REPORT: ${data.section}`, 
          content: data.content, timestamp: new Date().toLocaleTimeString(), type: 'report' 
        }]);
        break;
      case 'job.completed':
        setDecision({ action: data.decision || 'HOLD', confidence: '85%' });
        setIsAnalyzing(false);
        break;
    }
  };

  return (
    <div className={cn("flex h-screen w-full transition-colors duration-500 font-mono overflow-hidden relative", isDarkMode ? "bg-[#0B0E14] text-gray-300" : "bg-gray-50 text-gray-900")}>
      
      {/* --- Left: Agent Progress (Slimmer) --- */}
      <aside className={cn("w-64 border-r flex flex-col z-10 transition-colors duration-500", isDarkMode ? "bg-[#0F1219]/90 border-gray-800/50" : "bg-white border-gray-200")}>
        <div className="p-5 border-b border-inherit flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Cpu className="w-4 h-4 text-blue-500" />
            <span className="font-bold text-[10px] tracking-widest uppercase">12-Agent Nodes</span>
          </div>
          <button
            onClick={() => {
              setThemeMode('manual');
              setIsDarkMode(!isDarkMode);
            }}
            title={themeMode === 'auto' ? '当前：自动主题（点击切手动）' : '当前：手动主题'}
            className="hover:text-blue-500 transition-colors"
          >
            {isDarkMode ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-4 space-y-1">
          {agents.map((agent) => (
            <div key={agent.name} className={cn("flex items-center gap-3 p-2 rounded transition-all", agent.status === 'in_progress' ? "bg-blue-500/10 ring-1 ring-blue-500/20" : "")}>
              <div className={cn("w-1.5 h-1.5 rounded-full transition-all duration-500", agent.status === 'in_progress' ? "bg-blue-500 animate-pulse" : agent.status === 'completed' ? "bg-emerald-500" : "bg-gray-700")} />
              <div className="flex flex-col min-w-0">
                <span className={cn("text-[10px] font-bold truncate", agent.status === 'in_progress' ? "text-blue-500" : "text-inherit opacity-60")}>{agent.displayName}</span>
              </div>
            </div>
          ))}
        </div>
      </aside>

      {/* --- Center: Visualization & Reasoning --- */}
      <main className="flex-1 flex flex-col min-w-0 relative">
        <div className="flex-1 flex flex-col p-6 space-y-6 overflow-hidden">
          {/* Main Chart */}
          <section className={cn("h-1/2 min-h-[360px] rounded-3xl border relative transition-all duration-500 overflow-hidden", isDarkMode ? "bg-[#0F1219]/40 border-gray-800/30" : "bg-white border-gray-100 shadow-sm")}>
            <div ref={chartContainerRef} className="w-full h-full" />
            {!currentSymbol && (
              <div className="absolute inset-0 flex flex-col items-center justify-center opacity-20">
                 <BarChart3 className="w-12 h-12 text-blue-500 mb-2" />
                 <span className="text-[10px] font-black tracking-widest">ALPHA TERMINAL STANDBY</span>
              </div>
            )}
            {currentSymbol && (
              <div className="absolute top-6 left-6 flex items-center gap-3 bg-black/40 backdrop-blur-md px-4 py-2 rounded-2xl border border-white/5">
                <div className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
                <span className="text-sm font-black tracking-tighter text-white">{currentSymbol}</span>
              </div>
            )}
          </section>

          {/* Reasoning Logs (Detailed Process) */}
          <section className={cn("flex-1 rounded-3xl border flex flex-col overflow-hidden transition-colors duration-500", isDarkMode ? "bg-[#0F1219]/60 border-gray-800/30" : "bg-white border-gray-200 shadow-xl")}>
            <div className="px-6 py-3 border-b border-inherit bg-black/5 flex items-center justify-between">
              <span className="text-[10px] font-black uppercase tracking-widest opacity-50">Multi-Agent Deep Thought</span>
              <Terminal className="w-3 h-3 opacity-30" />
            </div>
            <div className="flex-1 overflow-y-auto custom-scrollbar p-6 font-mono space-y-4">
              {messages.filter(m => m.role === 'assistant' && m.type === 'text').map((msg, idx) => (
                <div key={idx} className="flex flex-col gap-1 border-l border-gray-800/50 pl-4 py-1">
                  <div className="text-[9px] font-black text-blue-500">{localizeAgent(msg.agent)}</div>
                  <div className="text-[12px] leading-relaxed opacity-80">{msg.content}</div>
                </div>
              ))}
              {messages.length === 0 && <div className="h-full flex items-center justify-center opacity-5 italic text-sm text-center px-20">Detailed logical deduction logs will stream here during execution...</div>}
            </div>
          </section>
        </div>
      </main>

      {/* --- Right: Alpha Copilot (Dialogue & Decision) --- */}
      <aside className={cn("w-[520px] border-l flex flex-col z-10 transition-colors duration-500", isDarkMode ? "bg-[#0B0E14] border-gray-800/50" : "bg-gray-100 border-gray-200")}>
        
        {/* Final Decision Pin */}
        <div className="p-8 border-b border-inherit">
          <div className={cn(
            "p-8 rounded-[32px] border-2 flex flex-col items-center gap-3 transition-all duration-1000",
            decision?.action === 'BUY' ? 'border-emerald-500 bg-emerald-500/10 shadow-[0_0_40px_rgba(16,185,129,0.15)]' :
            decision?.action === 'SELL' ? 'border-red-500 bg-red-500/10 shadow-[0_0_40px_rgba(239,68,68,0.15)]' :
            "border-gray-800 opacity-40 grayscale"
          )}>
            <span className="text-[10px] font-black tracking-[0.3em] opacity-50 uppercase">Signal Protocol Alpha</span>
            <div className={cn(
              "text-7xl font-black italic tracking-tighter transition-all leading-none",
              decision?.action === 'BUY' ? 'text-emerald-500' : decision?.action === 'SELL' ? 'text-red-500' : 'text-gray-500'
            )}>{decision?.action || 'VOID'}</div>
            {decision && <div className="text-[11px] font-bold px-4 py-1.5 bg-black/30 rounded-full border border-white/10 mt-2">{decision.confidence} Confidence Level</div>}
          </div>
        </div>

        {/* Conversation Thread */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto p-8 space-y-8 custom-scrollbar">
          {messages.filter(m => m.role === 'user' || m.type === 'report').map((msg, idx) => (
            <div key={idx} className={cn("flex flex-col gap-3 animate-in fade-in slide-in-from-bottom-2 duration-300", msg.role === 'user' ? "items-end" : "items-start")}>
              <div className={cn(
                "max-w-[95%] p-5 rounded-[24px] text-[13px] leading-relaxed transition-all",
                msg.role === 'user' 
                  ? "bg-blue-600 text-white rounded-tr-none shadow-xl shadow-blue-900/30" 
                  : (isDarkMode ? "bg-gray-800/40 border border-gray-700/50 rounded-tl-none" : "bg-white border border-gray-200 rounded-tl-none shadow-md")
              )}>
                {msg.agent && <div className={cn("text-[9px] font-black mb-3 opacity-50 tracking-widest flex items-center gap-2", msg.role === 'user' ? "justify-end" : "")}>
                  {!msg.agent.includes('REPORT') && <Bot className="w-3 h-3" />}
                  {localizeAgent(msg.agent)}
                </div>}
                <div className={cn("prose prose-sm max-w-none", isDarkMode ? "prose-invert" : "")}>
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
                </div>
              </div>
              <span className="text-[9px] opacity-20 px-2 font-black tracking-tighter">{msg.timestamp}</span>
            </div>
          ))}
          {messages.length === 0 && (
            <div className="h-full flex flex-col items-center justify-center text-center space-y-6 opacity-20">
              <div className="relative">
                <div className="absolute inset-0 bg-blue-500 blur-3xl opacity-20 animate-pulse" />
                <Bot className="w-16 h-16 relative" />
              </div>
              <div className="space-y-2">
                <p className="text-[11px] font-black tracking-[0.4em] uppercase">Alpha Copilot 4.0</p>
                <p className="text-[9px] font-medium italic">Systems primed. Awaiting intelligence prompt.</p>
              </div>
            </div>
          )}
        </div>

        {/* Dialogue Input */}
        <div className="p-8 bg-inherit border-t border-inherit">
          <div className={cn(
            "relative group flex items-center transition-all border rounded-[20px] overflow-hidden",
            isDarkMode ? "bg-gray-900 border-gray-800 focus-within:border-blue-500/50 shadow-2xl" : "bg-white border-gray-200 focus-within:border-blue-500 shadow-lg"
          )}>
            <input 
              className="flex-1 bg-transparent py-5 pl-6 pr-14 text-[14px] focus:outline-none placeholder:text-gray-600 font-medium"
              placeholder="Command the swarm..."
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && startAnalysis()}
            />
            <button 
              onClick={startAnalysis}
              disabled={isAnalyzing}
              className={cn(
                "absolute right-4 p-2.5 rounded-xl transition-all",
                isAnalyzing ? "text-gray-600 animate-spin" : "text-blue-500 hover:bg-blue-500 hover:text-white active:scale-90"
              )}
            >
              {isAnalyzing ? <Activity className="w-5 h-5" /> : <Send className="w-5 h-5" />}
            </button>
          </div>
          <div className="mt-4 flex justify-between items-center px-2">
            <p className="text-[9px] opacity-20 font-black tracking-widest uppercase">Proprietary Neural Link</p>
            <div className="flex gap-1">
              <div className="w-1 h-1 rounded-full bg-blue-500/40" />
              <div className="w-1 h-1 rounded-full bg-blue-500/40" />
              <div className="w-1 h-1 rounded-full bg-blue-500/40" />
            </div>
          </div>
        </div>
      </aside>

    </div>
  );
}
