import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { OptimizationProposalPanel } from "../components/optimization-proposal";
import type { FeedbackAnalysis, OptimizationProposal } from "../lib/api";
const mocks = vi.hoisted(() => ({list:vi.fn(), get:vi.fn(), approve:vi.fn(), reject:vi.fn()}));
vi.mock("@/lib/api", async (original) => ({...await original<object>(), optimizationApi:mocks}));
const analysis={id:"analysis",campaign_id:"campaign",status:"ACCEPTED"} as FeedbackAnalysis;
let proposal:OptimizationProposal;
beforeEach(() => {
 vi.clearAllMocks();
 proposal={id:"proposal",campaign_id:"campaign",feedback_analysis_id:"analysis",strategy_version:7,status:"WAITING_APPROVAL",summary:"Summary",created_by_agent_run_id:"run",reviewed_by_user_id:null,reviewed_at:null,created_at:"2026-10-07",actions:[
 {id:"one",position:0,type:"CONTENT_REVISION",target_entity_type:"CONTENT_ITEM",target_entity_id:"content",target_version_id:"version",reason:"Уточнить аргумент",expected_effect:"Повысить ясность",priority:"HIGH",evidence_refs:[{type:"marketing_feedback",id:"feedback"}],status:"PROPOSED"},
 {id:"two",position:1,type:"NO_CHANGE",target_entity_type:"CAMPAIGN",target_entity_id:"campaign",target_version_id:null,reason:"Недостаточно доказательств",expected_effect:"Сохранить подход",priority:"LOW",evidence_refs:[],status:"PROPOSED"}]};
 mocks.list.mockImplementation(async()=>[structuredClone(proposal)]);mocks.get.mockImplementation(async()=>structuredClone(proposal));
 mocks.approve.mockImplementation(async(id)=>{proposal.actions.find(a=>a.id===id)!.status="APPROVED";});mocks.reject.mockImplementation(async(id)=>{proposal.actions.find(a=>a.id===id)!.status="REJECTED";});
});
async function ready(archived=false){render(<OptimizationProposalPanel analysis={analysis} archived={archived}/>);await screen.findByRole("heading",{name:"Рекомендации к изменениям"});}
it("loads accepted proposal with separate cards, target, reason, effect, priority and evidence",async()=>{
 await ready();expect(mocks.list).toHaveBeenCalledWith("campaign");const card=screen.getByRole("article",{name:"Доработка контента"});
 for(const text of ["Уточнить аргумент","Повысить ясность",/HIGH/,/Обратная связь: feedback/,/content.*version/])expect(within(card).getByText(text)).toBeInTheDocument();expect(screen.getAllByRole("article")).toHaveLength(2);
});
it("approves one action and refreshes terminal decision",async()=>{
 await ready();fireEvent.click(within(screen.getByRole("article",{name:"Доработка контента"})).getByRole("button",{name:"Принять"}));await waitFor(()=>expect(mocks.get).toHaveBeenCalledWith("proposal"));
 const card=screen.getByRole("article",{name:"Доработка контента"});await within(card).findByText(/Принято/);for(const button of within(card).getAllByRole("button"))expect(button).toBeDisabled();expect(mocks.approve).toHaveBeenCalledWith("one");expect(mocks.reject).not.toHaveBeenCalled();expect(screen.getByRole("article",{name:"Без изменений"})).toHaveTextContent("Ожидает решения");
});
it("rejects one action without approving others",async()=>{await ready();fireEvent.click(within(screen.getByRole("article",{name:"Без изменений"})).getByRole("button",{name:"Отклонить"}));await waitFor(()=>expect(mocks.reject).toHaveBeenCalledWith("two"));await within(screen.getByRole("article",{name:"Без изменений"})).findByText(/Отклонено/);expect(mocks.approve).not.toHaveBeenCalled();});
it("persists terminal state after page reload",async()=>{proposal.actions[0].status="APPROVED";const view=render(<OptimizationProposalPanel analysis={analysis} archived={false}/>);await screen.findByText(/Принято/);view.unmount();await ready();for(const button of within(screen.getByRole("article",{name:"Доработка контента"})).getAllByRole("button"))expect(button).toBeDisabled();});
it("legacy has no fabricated proposal UI",async()=>{mocks.list.mockResolvedValue([]);const{container}=render(<OptimizationProposalPanel analysis={analysis} archived={false}/>);await waitFor(()=>expect(mocks.list).toHaveBeenCalled());expect(container).toBeEmptyDOMElement();});
it("shows advisory notice with no bulk or Apply controls",async()=>{await ready();expect(screen.getByText(/Принятие рекомендации пока только фиксирует решение/)).toBeInTheDocument();for(const name of ["Принять всё","Применить","Изменить стратегию","Создать задачу","Пересобрать медиаплан","Создать эксперимент","Опубликовать"])expect(screen.queryByRole("button",{name})).not.toBeInTheDocument();});
it("archived is read only",async()=>{await ready(true);for(const button of screen.getAllByRole("button"))expect(button).toBeDisabled();});
it("renders untrusted reason as text and no arbitrary links",async()=>{proposal.actions[0].reason='<a href="https://evil.test">change</a>';await ready();expect(screen.getByText(proposal.actions[0].reason)).toBeInTheDocument();expect(screen.queryByRole("link")).not.toBeInTheDocument();});
it("load failure visible rather than treated as legacy",async()=>{mocks.list.mockRejectedValue(new Error("Load failed"));render(<OptimizationProposalPanel analysis={analysis} archived={false}/>);await screen.findByRole("alert");});
it("decision failure produces no optimistic success or automatic retry",async()=>{mocks.approve.mockRejectedValue(new Error("Conflict"));await ready();fireEvent.click(within(screen.getByRole("article",{name:"Доработка контента"})).getByRole("button",{name:"Принять"}));await screen.findByRole("alert");expect(mocks.approve).toHaveBeenCalledTimes(1);expect(mocks.get).not.toHaveBeenCalled();expect(screen.getAllByText(/Ожидает решения/)).toHaveLength(2);});
