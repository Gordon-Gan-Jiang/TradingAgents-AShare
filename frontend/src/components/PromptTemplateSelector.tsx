import type { PromptTemplate } from '@/types'

type PromptTemplateSelectorProps = {
    label?: string
    value: string
    templates: PromptTemplate[]
    loading?: boolean
    onChange: (templateId: string) => void
    className?: string
}

export default function PromptTemplateSelector({
    label = '深度分析模板',
    value,
    templates,
    loading = false,
    onChange,
    className = '',
}: PromptTemplateSelectorProps) {
    return (
        <label className={`inline-flex items-center gap-2 text-xs text-slate-500 dark:text-slate-300 ${className}`}>
            <span>{label}</span>
            <select
                value={value}
                onChange={e => onChange(e.target.value)}
                disabled={loading || templates.length === 0}
                className="h-8 max-w-[220px] rounded-lg border border-slate-300 bg-white px-2 text-xs text-slate-700 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200"
            >
                {templates.map(template => (
                    <option key={template.id} value={template.id}>
                        {template.is_builtin ? `系统 · ${template.name}` : `自定义 · ${template.name}`}
                    </option>
                ))}
            </select>
        </label>
    )
}
