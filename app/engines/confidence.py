"""
Engine for calculating confidence scores for trip recommendations.
"""
from dataclasses import dataclass, field
from typing import Dict, Any


@dataclass
class ConfidenceResult:
    score: int
    label: str = "High"
    factors: Dict[str, Any] = field(default_factory=dict)


class ConfidenceEngine:
    def __init__(self):
        pass

    def score(
        self,
        weather_score: int = 50,
        road_score: int = 50,
        wildlife_score: int = 50,
        operator_score: int = 50,
        budget_score: int = 50,
    ) -> ConfidenceResult:
        """
        Calculates an overall confidence score based on component engine sub-scores.
        """
        # Weighted aggregate score calculation
        total_score = int(
            (weather_score * 0.20)
            + (road_score * 0.15)
            + (wildlife_score * 0.25)
            + (operator_score * 0.20)
            + (budget_score * 0.20)
        )

        label = (
            "High" if total_score >= 80
            else "Medium" if total_score >= 60
            else "Low"
        )

        factors = {
            "weather": weather_score,
            "road": road_score,
            "wildlife": wildlife_score,
            "operator": operator_score,
            "budget": budget_score,
        }

        return ConfidenceResult(
            score=total_score,
            label=label,
            factors=factors,
        )

    def calculate_confidence(self, data: Dict[str, Any]) -> float:
        """
        Legacy fallback method preserved for backwards compatibility.
        """
        return 0.95
