import { FreshnessBadge } from '@/components/FreshnessBadge'
import { shouldShowFreshnessBadge, type FreshnessStatus } from '@/components/freshnessListDisplay'

export function FreshnessStatusCell({ status }: { status?: FreshnessStatus }) {
    if (shouldShowFreshnessBadge(status)) {
        return <FreshnessBadge status={status} />
    }
    return <span className="text-slate-400 dark:text-slate-500">—</span>
}
