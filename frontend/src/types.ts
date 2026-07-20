export type Tab = "recommend" | "record" | "history" | "wishlist" | "notInterested";
export type RecommendationStrategy = "hybrid" | "bandit_hybrid";
export type ThemeMode = "system" | "light" | "dark";

export type CandidateQueueStatus = {
  pending_count: number;
  failed_count: number;
  processing: boolean;
  processed_count: number;
  current_subject_id: string | null;
  current_source_label: string | null;
  current_source_ref: string | null;
  blocked_for_run: boolean;
  failure_reason: string | null;
  last_error: string | null;
};

export type SearchCandidate = {
  subject_id: string;
  title: string;
  year: number | null;
  director: string | null;
  url: string | null;
};

export type Movie = {
  id: string;
  title: string;
  year: number;
  director: string;
  directors: string[];
  main_cast: string[];
  cast: string[];
  douban_rating: number;
  douban_url: string;
  poster_url: string | null;
};

export type RecommendationItem = {
  id: string;
  rank: number;
  slot_type: "exploit" | "explore";
  score: number;
  score_components: Record<string, unknown>;
  source_ref: string | null;
  source_label: string | null;
  processing_status: string | null;
  processed_at: string | null;
  movie: Movie;
};

export type RecommendationSession = {
  id: string;
  strategy: string;
  created_at: string;
  debug_metadata?: Record<string, unknown>;
  items: RecommendationItem[];
};

export type WishlistItem = {
  id: string;
  status: string;
  source_session_id: string;
  score: number | null;
  source_ref: string | null;
  source_label: string | null;
  created_at: string;
  closed_at: string | null;
  movie: Movie;
};

export type NotInterestedItem = {
  id: string;
  movie_id: string;
  state: "not_interested";
  state_changed_at: string;
  session_id: string;
  item_id: string;
  movie: Movie;
};

export type RecordForm = {
  history_id: string;
  watched_date: string;
  rating: string;
  quality: "1080p" | "4K" | "Other";
  custom_quality: string;
  comment: string;
};

export type ViewingHistoryItem = {
  id: string;
  title: string;
  year: number | null;
  directors: string[];
  watched_date: string;
  user_rating: number;
  quality: string | null;
  comment: string | null;
  sync_state: "synced" | "pending" | "failed";
  sync_attempts: number;
  sync_error: string | null;
};

export type ViewingHistoryResponse = PagedResponse<ViewingHistoryItem> & {
  years: number[];
};

export type RecordHandoff = {
  movie: SearchCandidate;
  sourceTab: Tab;
  session_id?: string;
  recommendation_item_id?: string;
  wishlist_id?: string;
};

export type ProcessedRecommendationItem = {
  session_id: string;
  recommendation_item_id: string;
  processing_status: string | null;
  processed_at: string | null;
};

export type RecordViewingHistoryResponse = {
  session_id?: string;
  recommendation_item_id?: string;
  processing_status?: string | null;
  processed_at?: string | null;
  sync_state: "synced" | "pending" | "failed";
};

export type UndoRecommendationProcessingResponse = Pick<
  RecommendationItem,
  "id" | "processing_status" | "processed_at"
>;

export type PagedResponse<T> = {
  items: T[];
  total: number;
};
