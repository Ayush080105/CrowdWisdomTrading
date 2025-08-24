import yaml
import json
from crewai import Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task
from crowdwisdom.tools.scrape_mcp_tool import scrape_mcp_site
 # Playwright-based scraper

# --- Helper function for logging ---
def debug_log(stage: str, data):
    """Pretty print intermediate results for debugging"""
    print(f"\n\n🔎 DEBUG: {stage} output")
    try:
        print(json.dumps(data, indent=2))
    except Exception:
        print(data)
    print("-" * 50)
    return data


# --- Crew Definition ---
@CrewBase
class Crowdwisdom():
    """CrowdwisdomTrading Aggregator Crew using MCPPlaywright"""

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    # --- Agents ---
    @agent
    def data_collector(self) -> Agent:
        """Collects prediction market data using MCPPlaywright"""
        return Agent(
            config=self.agents_config["data_collector"],
            tools=[scrape_mcp_site],  
            verbose=True
        )

    @agent
    def product_identifier(self) -> Agent:
        """Matches equivalent products from different websites"""
        return Agent(
            config=self.agents_config["product_identifier"],
            verbose=True
        )

    @agent
    def data_organizer(self) -> Agent:
        """Organizes unified products into CSV for stakeholders"""
        return Agent(
            config=self.agents_config["data_organizer"],
            verbose=True
        )

    # --- Tasks ---
    @task
    def scrape_data(self) -> Task:
        """Scrape data from prediction markets"""
        return Task(
            config=self.tasks_config["scrape_data"],
            intermediate_output=True,  # logs each step
        )

    @task
    def identify_products(self) -> Task:
        """Unify and match scraped products"""
        return Task(
            config=self.tasks_config["identify_products"],
            callback=lambda output: debug_log("Identify Products", output),
        )

    @task
    def organize_data(self) -> Task:
        """Reorganize and export final data as CSV"""
        return Task(
            config=self.tasks_config["organize_data"],
            output_file="unified_products.csv",
            callback=lambda output: debug_log("Organize Data", output),
        )

    # --- Crew Pipeline ---
    @crew
    def crew(self) -> Crew:
        """Creates the Crowdwisdom crew pipeline"""
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
        )
