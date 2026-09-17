% Candidate support, not proof of the injected mechanism.
candidate(Entity, Family, Evidence) :-
    observation(Evidence, Entity, Family, _, Score, _, _, _), Score >= 4.
colocated(A,B) :- hosted_on(A,N), hosted_on(B,N), A \= B.
overlap(A,B) :-
    observation(A,_,_,_,_,AS,AE,_), observation(B,_,_,_,_,BS,BE,_),
    AS =< BE + 60000, BS =< AE + 60000.
pod_local_support(Pod,Family,[Positive,Negative]) :-
    hosted_on(Pod,Node), candidate(Pod,Family,Positive), normal(Node,Family,Negative).

