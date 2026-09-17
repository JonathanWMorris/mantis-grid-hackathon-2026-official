:- use_module(library(http/json)).
:- use_module(library(solution_sequences)).
:- use_module(library(time)).
:- dynamic hosted_on/2, calls/2, observation/8, normal/3, trace_change/3, log_pattern/3.
:- table depends_on/2.
depends_on(A,B) :- calls(A,B).
depends_on(A,C) :- calls(A,B), depends_on(B,C).

:- use_module(library(aggregate)).
:- use_module(library(terms)).
:- consult('trusted.pl').

proposed_head(Head) :- callable(Head), functor(Head,Name,Arity),
    atom_concat(h_,_,Name), Arity =< 10.
data_arg(X) :- var(X), !.
data_arg(X) :- atomic(X), !.
data_arg(X) :- is_list(X), maplist(data_arg,X).
data_args(G) :- G =.. [_|Args], maplist(data_arg,Args).
signature(hosted_on,2). signature(calls,2). signature(depends_on,2).
signature(observation,8). signature(normal,3). signature(trace_change,3).
signature(log_pattern,3). signature(candidate,3). signature(colocated,2).
signature(overlap,2). signature(pod_local_support,3).
signature(length,2). signature(sort,2). signature(sum_list,2).
signature(min_list,2). signature(max_list,2).

arith_expr(X) :- var(X), !.
arith_expr(X) :- number(X), !.
arith_expr(X) :- nonvar(X), X =.. [F|Args], length(Args,A),
    memberchk(F/A,[(+)/1,(-)/1,(+)/2,(-)/2,(*)/2,(/)/2,(//)/2,mod/2,abs/1,min/2,max/2]),
    maplist(arith_expr,Args).
aggregate_template(count).
aggregate_template(T) :- nonvar(T), T =.. [F,E], memberchk(F,[sum,min,max]), arith_expr(E).

allowed_goal(G) :- nonvar(G), checked_goal(G).
checked_goal((A,B)) :- !, allowed_goal(A), allowed_goal(B).
checked_goal((A;B)) :- !, allowed_goal(A), allowed_goal(B).
checked_goal(findall(T,G,L)) :- !, data_arg(T), allowed_goal(G), data_arg(L).
checked_goal(aggregate_all(T,G,R)) :- !, aggregate_template(T), allowed_goal(G), data_arg(R).
checked_goal(G) :- functor(G,F,2), memberchk(F,[is,>,<,>=,=<,=:=,=\=]), !,
    G =.. [_,A,B], arith_expr(A), arith_expr(B).
checked_goal(G) :- proposed_head(G), !, data_args(G).
checked_goal(G) :- functor(G,N,A),
    (signature(N,A) ; memberchk(N/A,[(=)/2,(\=)/2,(==)/2,true/0])), data_args(G).

validate_goal(G) :- (allowed_goal(G) -> true ; goal_error(G)).
goal_error(G) :- nonvar(G), functor(G,N,A), signature(N,Expected), A \= Expected, !,
    throw(error(wrong_arity(N,A,Expected),G)).
goal_error(G) :- nonvar(G), (G=(A,B);G=(A;B)), !, validate_goal(A), validate_goal(B).
goal_error(G) :- throw(error(unsupported_goal_or_unquoted_atom,G)).

parse_one(Text,Term,Options) :-
    setup_call_cleanup(open_string(Text,Stream),
      (read_term(Stream,Term,[syntax_errors(error)|Options]),
       read_term(Stream,Extra,[syntax_errors(error)]),
       (Extra == end_of_file -> true ; throw(error(expected_one_term,Text)))), close(Stream)).

install_rule(Text) :-
    parse_one(Text,Term,[]),
    (Term=(Head :- Body) -> true ; Head=Term, Body=true),
    (proposed_head(Head), data_args(Head) -> true ; throw(error(disallowed_rule,Text))),
    validate_goal(Body),
    assertz((Head :- Body)), functor(Head,N,A), table(N/A).

binding(Name=Value,Name-String) :-
    term_size(Value,Size),
    (Size =< 10000 -> true ; throw(error(nested_result_limit,Name))),
    term_string(Value,String,[quoted(true)]), string_length(String,Length),
    (Length =< 100000 -> true ; throw(error(nested_result_limit,Name))).
ground_binding(_=Value) :- ground(Value).
bounded_binding(Name=Value) :- term_size(Value,Size), (Size =< 10000 -> true ; throw(error(nested_result_limit,Name))).
bindings_json(Bindings,Dict) :- maplist(bounded_binding,Bindings), include(ground_binding,Bindings,Ground), maplist(binding,Ground,Pairs), dict_pairs(Dict,bindings,Pairs).
run_request(Request,Result) :-
    maplist(install_rule,Request.rules),
    parse_one(Request.query,Query,[variable_names(Bindings)]),
    validate_goal(Query),
    call_with_time_limit(3, findnsols(101,Bindings,Query,Solutions)),
    maplist(bindings_json,Solutions,Rows),
    term_string(Rows,Encoded), string_length(Encoded,OutputSize),
    (OutputSize =< 500000 -> true ; throw(error(result_size_limit,OutputSize))),
    Result=_{status:ok,bindings:Rows}.

main :- current_prolog_flag(argv,[Facts,RequestFile|_]),
    consult(Facts), open(RequestFile,read,In), json_read_dict(In,Request), close(In),
    catch(run_request(Request,Result),Error,
          (term_string(Error,Message),Result=_{status:error,error:Message})),
    json_write_dict(current_output,Result), nl, halt.
:- initialization(main,main).
